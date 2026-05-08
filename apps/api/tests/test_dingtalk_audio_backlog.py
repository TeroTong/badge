from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from smart_badge_api.asr.tencent_request_audit import append_tencent_request_event
from smart_badge_api.asr.tencent_task_registry import (
    get_tencent_task_registry_entry,
    upsert_tencent_task_registry_entry,
)
from smart_badge_api.core.config import get_settings
from smart_badge_api.dingtalk_audio_archive import get_archive_root
from smart_badge_api.dingtalk_audio_backlog import (
    DeviceProfile,
    _failed_manifest_recovery_decision,
    sync_dingtalk_audio_archive_backlog,
)
from smart_badge_api.dingtalk_audio_sync import _ensure_stage_paths, _manifest_path, _read_manifest, _stage_key, _write_manifest


def test_sync_dingtalk_audio_archive_backlog_stages_and_processes_new_archive_item(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN100" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "10_101010.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "10_101010.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN100",
                    "fileId": "file-100",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 186000,
                    "fileSize": 2048,
                    "createTimeMs": 1775787010000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        async def fake_execute(stage_key: str) -> None:
            paths = _ensure_stage_paths()
            manifest = _read_manifest(paths, stage_key)
            assert manifest is not None
            manifest["status"] = "analyzed"
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(
                        return_value={
                            "SN100": DeviceProfile(
                                device_id="device100",
                                staff_id="staff100",
                                staff_name="顾问甲",
                                staff_role="consultant",
                            )
                        }
                    ),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ),
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=True)

            stage_key = _stage_key("SN100", "file-100")
            manifest = json.loads(_manifest_path(_ensure_stage_paths(), stage_key).read_text(encoding="utf-8"))
            assert manifest["staffName"] == "顾问甲"
            assert manifest["audioPath"] == str(audio_path)
            assert manifest["status"] == "analyzed"

            assert result.archive_items == 1
            assert result.staged_new == 1
            assert result.already_staged == 0
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
            assert result.final_archive_status["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_keeps_short_filtered_archive_unprocessed(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN101" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "10_101011.mp3"
        metadata_path = archive_dir / "10_101011.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN101",
                    "fileId": "file-101",
                    "remoteFileName": "short.mp3",
                    "durationMs": 59000,
                    "durationSeconds": 59,
                    "fileSize": 512,
                    "createTimeMs": 1775787011000,
                    "filteredAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                    "status": "filtered",
                    "qualityStage": "pre_asr",
                    "qualityReason": "录音时长 59 秒，低于最小时长 60 秒",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=AssertionError("filtered archive item should not enter pipeline")),
                ),
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=True)

            stage_key = _stage_key("SN101", "file-101")
            manifest = json.loads(_manifest_path(_ensure_stage_paths(), stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "filtered"
            assert manifest["qualityStage"] == "pre_asr"
            assert manifest["qualityReason"] == "录音时长 59 秒，低于最小时长 60 秒"

            assert result.archive_items == 1
            assert result.staged_new == 1
            assert result.processed_now == 0
            assert result.final_archive_status["filtered"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_retries_failed_manifest(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN200" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "11_111111.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "11_111111.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN200",
                    "fileId": "file-200",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN200", "file-200")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN200",
                "fileId": "file-200",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "old error",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )

        async def fake_execute(stage_key: str) -> None:
            manifest = _read_manifest(paths, stage_key)
            assert manifest is not None
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ),
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=True)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            assert "errorMessage" not in manifest

            assert result.archive_items == 1
            assert result.staged_new == 0
            assert result.already_staged == 1
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
            assert result.final_archive_status["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_retries_safe_pre_submit_failed_manifest_even_when_retry_failed_disabled(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("TENCENT_ASR_TASK_REGISTRY_PATH", str(tmp_path / "asr" / "registry.json"))
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN250" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "11_121212.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "11_121212.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN250",
                    "fileId": "file-250",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN250", "file-250")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN250",
                "fileId": "file-250",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": f"腾讯云 ASR 分片转码失败：{audio_path.name}",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )

        async def fake_execute(current_stage_key: str) -> None:
            manifest = _read_manifest(paths, current_stage_key)
            assert manifest is not None
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ),
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            assert "errorMessage" not in manifest
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_failed_manifest_with_transcript_and_without_analysis_result_is_retryable(tmp_path) -> None:
    transcript_path = tmp_path / "sample.transcript.json"
    transcript_path.write_text(
        json.dumps({"fullText": "您好", "utterances": [{"speaker": "consultant", "text": "您好"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    decision = _failed_manifest_recovery_decision(
        {
            "stageKey": "SN300__file-300",
            "status": "failed",
            "transcriptPath": str(transcript_path),
            "analysisResultPath": None,
            "errorMessage": "Expecting value: line 1 column 1 (char 0)",
        }
    )

    assert decision.mode == "retry_analysis_only"


def test_sync_dingtalk_audio_archive_backlog_retries_failed_manifest_without_any_tencent_submit_trace(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("TENCENT_ASR_TASK_REGISTRY_PATH", str(tmp_path / "asr" / "registry.json"))
        monkeypatch.setenv("TENCENT_ASR_REQUEST_AUDIT_LOG_PATH", str(tmp_path / "asr" / "requests.jsonl"))
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN255" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "11_131313.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "11_131313.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN255",
                    "fileId": "file-255",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN255", "file-255")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN255",
                "fileId": "file-255",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "录音处理在 transcribing 阶段超过 900 秒未完成，已自动标记为失败",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )

        async def fake_execute(current_stage_key: str) -> None:
            manifest = _read_manifest(paths, current_stage_key)
            assert manifest is not None
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ),
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            assert "errorMessage" not in manifest
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_retries_stage_only_failed_manifest(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_dir = paths.audio_dir / "SN257"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_dir / "13_141414.mp3"
        audio_path.write_bytes(b"ID3demo")
        stage_key = _stage_key("SN257", "file-257")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN257",
                "fileId": "file-257",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "腾讯云 ASR 请求失败：CreateRecTask",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )

        async def fake_execute(current_stage_key: str) -> None:
            manifest = _read_manifest(paths, current_stage_key)
            assert manifest is not None
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=True)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            assert "errorMessage" not in manifest
            execute_mock.assert_called_once_with(stage_key)

            assert result.archive_items == 0
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_recovers_stale_transcribing_manifest(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_STALE_PROCESSING_TIMEOUT_SECONDS", "60")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN300" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "12_121212.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "12_121212.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN300",
                    "fileId": "file-300",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 186000,
                    "fileSize": 2048,
                    "createTimeMs": 1775787010000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN300", "file-300")
        _manifest_path(paths, stage_key).write_text(
            json.dumps(
                {
                    "stageKey": stage_key,
                    "deviceCode": "SN300",
                    "fileId": "file-300",
                    "stagedFileName": audio_path.name,
                    "audioPath": str(audio_path),
                    "status": "transcribing",
                    "createdAt": "2026-04-13T00:00:00+00:00",
                    "updatedAt": "2026-04-13T00:01:00+00:00",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "failed"
            assert "超过 60 秒未完成" in manifest["errorMessage"]
            execute_mock.assert_called_once_with(stage_key)

            assert result.archive_items == 1
            assert result.staged_new == 0
            assert result.already_staged == 1
            assert result.processed_now == 1
            assert result.final_archive_status["failed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_recovers_stale_analyzing_manifest_with_result(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_STALE_PROCESSING_TIMEOUT_SECONDS", "60")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN350" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "12_131313.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "12_131313.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN350",
                    "fileId": "file-350",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 186000,
                    "fileSize": 2048,
                    "createTimeMs": 1775787010000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        result_path = paths.result_dir / "file-350.result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8")
        stage_key = _stage_key("SN350", "file-350")
        _manifest_path(paths, stage_key).write_text(
            json.dumps(
                {
                    "stageKey": stage_key,
                    "deviceCode": "SN350",
                    "fileId": "file-350",
                    "stagedFileName": audio_path.name,
                    "audioPath": str(audio_path),
                    "status": "analyzing",
                    "analysisResultPath": str(result_path),
                    "createdAt": "2026-04-13T00:00:00+00:00",
                    "updatedAt": "2026-04-13T00:01:00+00:00",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            assert manifest.get("errorMessage") is None
            execute_mock.assert_not_called()

            assert result.archive_items == 1
            assert result.staged_new == 0
            assert result.already_staged == 1
            assert result.processed_now == 0
            assert result.final_archive_status["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_recovers_orphaned_stale_manifest(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_STALE_PROCESSING_TIMEOUT_SECONDS", "60")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN400", "file-400")
        _manifest_path(paths, stage_key).write_text(
            json.dumps(
                {
                    "stageKey": stage_key,
                    "deviceCode": "SN400",
                    "fileId": "file-400",
                    "stagedFileName": "13_131313.mp3",
                    "audioPath": str(tmp_path / "missing.mp3"),
                    "status": "transcribing",
                    "createdAt": "2026-04-13T00:00:00+00:00",
                    "updatedAt": "2026-04-13T00:01:00+00:00",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "failed"
            assert "超过 60 秒未完成" in manifest["errorMessage"]
            execute_mock.assert_called_once_with(stage_key)

            assert result.archive_items == 0
            assert result.processed_now == 1
            assert result.process_summary["failed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_retries_failed_manifest_with_existing_tencent_task_id(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("TENCENT_ASR_TASK_REGISTRY_PATH", str(tmp_path / "asr" / "registry.json"))
        monkeypatch.setenv("TENCENT_ASR_REQUEST_AUDIT_LOG_PATH", str(tmp_path / "asr" / "requests.jsonl"))
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN450" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "14_141414.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "14_141414.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN450",
                    "fileId": "file-450",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN450", "file-450")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN450",
                "fileId": "file-450",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "录音处理在 transcribing 阶段超过 900 秒未完成，已自动标记为失败",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )
        await upsert_tencent_task_registry_entry(
            source_id=stage_key,
            chunk_index=1,
            chunk_count=3,
            audio_name=audio_path.name,
            audio_path=str(audio_path),
            status="submitted",
            request_id="req-450",
            task_id=15109260797,
            submitted_duration_ms=120000,
            recognized_duration_ms=None,
            error_code=None,
            error_message=None,
        )

        async def fake_execute(current_stage_key: str) -> None:
            manifest = _read_manifest(paths, current_stage_key)
            assert manifest is not None
            entry = get_tencent_task_registry_entry(
                source_id=current_stage_key,
                chunk_index=1,
                chunk_count=3,
            )
            assert entry is not None
            assert entry.get("task_id") == 15109260797
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            execute_mock.assert_called_once_with(stage_key)
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_clears_safe_submitting_registry_entry_before_retry(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("TENCENT_ASR_TASK_REGISTRY_PATH", str(tmp_path / "asr" / "registry.json"))
        monkeypatch.setenv("TENCENT_ASR_REQUEST_AUDIT_LOG_PATH", str(tmp_path / "asr" / "requests.jsonl"))
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN460" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "14_151515.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "14_151515.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN460",
                    "fileId": "file-460",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN460", "file-460")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN460",
                "fileId": "file-460",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "录音处理在 transcribing 阶段超过 900 秒未完成，已自动标记为失败",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )
        await upsert_tencent_task_registry_entry(
            source_id=stage_key,
            chunk_index=1,
            chunk_count=4,
            audio_name=audio_path.name,
            audio_path=str(audio_path),
            status="submitting",
            request_id=None,
            task_id=None,
            submitted_duration_ms=60000,
            recognized_duration_ms=None,
            error_code=None,
            error_message=None,
        )

        async def fake_execute(current_stage_key: str) -> None:
            manifest = _read_manifest(paths, current_stage_key)
            assert manifest is not None
            entry = get_tencent_task_registry_entry(
                source_id=current_stage_key,
                chunk_index=1,
                chunk_count=4,
            )
            assert entry is None
            manifest["status"] = "analyzed"
            manifest.pop("errorMessage", None)
            _write_manifest(paths, manifest)

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(side_effect=fake_execute),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "analyzed"
            execute_mock.assert_called_once_with(stage_key)
            assert result.processed_now == 1
            assert result.process_summary["analyzed"] == 1
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_archive_backlog_does_not_retry_submitting_registry_entry_when_submit_trace_exists(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("TENCENT_ASR_TASK_REGISTRY_PATH", str(tmp_path / "asr" / "registry.json"))
        monkeypatch.setenv("TENCENT_ASR_REQUEST_AUDIT_LOG_PATH", str(tmp_path / "asr" / "requests.jsonl"))
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN470" / "202604"
        archive_dir.mkdir(parents=True, exist_ok=True)
        audio_path = archive_dir / "14_161616.mp3"
        audio_path.write_bytes(b"ID3demo")
        metadata_path = archive_dir / "14_161616.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "sn": "SN470",
                    "fileId": "file-470",
                    "remoteFileName": "remote.mp3",
                    "durationMs": 240000,
                    "fileSize": 4096,
                    "createTimeMs": 1775873471000,
                    "downloadedAt": "2026-04-13T00:00:00+08:00",
                    "audioPath": str(audio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        paths = _ensure_stage_paths()
        stage_key = _stage_key("SN470", "file-470")
        _write_manifest(
            paths,
            {
                "stageKey": stage_key,
                "deviceCode": "SN470",
                "fileId": "file-470",
                "stagedFileName": audio_path.name,
                "audioPath": str(audio_path),
                "status": "failed",
                "errorMessage": "录音处理在 transcribing 阶段超过 900 秒未完成，已自动标记为失败",
                "createdAt": "2026-04-13T00:00:00+00:00",
            },
        )
        await upsert_tencent_task_registry_entry(
            source_id=stage_key,
            chunk_index=1,
            chunk_count=4,
            audio_name=audio_path.name,
            audio_path=str(audio_path),
            status="submitting",
            request_id=None,
            task_id=None,
            submitted_duration_ms=60000,
            recognized_duration_ms=None,
            error_code=None,
            error_message=None,
        )
        await append_tencent_request_event(
            occurred_at=datetime.now(timezone.utc),
            status="submitted",
            audio_name=audio_path.name,
            audio_path=str(audio_path),
            source_id=stage_key,
            chunk_index=1,
            chunk_count=4,
            submitted_duration_ms=60000,
            recognized_duration_ms=None,
            file_size_bytes=4096,
            request_id="req-470",
            task_id=15100000000,
            error_code=None,
            error_message=None,
        )

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_backlog._load_device_profiles",
                    AsyncMock(return_value={}),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_backlog.execute_dingtalk_recording_pipeline",
                    AsyncMock(),
                ) as execute_mock,
            ):
                result = await sync_dingtalk_audio_archive_backlog(workers=1, retry_failed=False)

            manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert manifest["status"] == "failed"
            execute_mock.assert_not_called()
            assert result.processed_now == 0
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())
