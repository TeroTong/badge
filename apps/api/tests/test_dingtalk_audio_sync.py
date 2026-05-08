from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Device, Recording, Staff, Transcript
from smart_badge_api.device_binding import bind_staff_to_device
from smart_badge_api.dingtalk_audio_sync import (
    _ensure_stage_paths,
    _manifest_path,
    _pre_asr_quality_decision,
    _stage_key,
    execute_dingtalk_recording_pipeline,
    sync_dingtalk_audio_files,
)


def _analysis_result_with_indication(score: int = 88) -> dict:
    return {
        "standardized_indications": {
            "summary": "识别出1项适应症：纹路（面部）",
            "items": [
                {
                    "department_code": "Y3",
                    "department_name": "皮肤",
                    "indication_code": "SYZ3002",
                    "indication_name": "纹路",
                    "body_part_code": "BW3001",
                    "body_part_name": "面部",
                    "evidence": "[00:03] 我主要想改善法令纹",
                }
            ],
        },
        "consultation_evaluation": {"overall_score": score},
    }


def test_pre_asr_quality_defaults_to_one_minute(monkeypatch) -> None:
    monkeypatch.delenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", raising=False)
    get_settings.cache_clear()
    try:
        decision = _pre_asr_quality_decision(59)
        assert decision.passed is False
        assert decision.stage == "pre_asr"
        assert "低于最小时长 60 秒" in (decision.reason or "")
        assert _pre_asr_quality_decision(60).passed is True
    finally:
        get_settings.cache_clear()


def test_sync_dingtalk_audio_files_stages_audio_and_creates_placeholder_recording(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(name="钟露", external_account="86000995", permission_role="staff", is_active=True)
                db.add(staff)
                await db.flush()
                device = Device(name="八号工牌", device_code="SSYX41022508", staff_id=staff.id, is_active=True)
                db.add(device)
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_list_audio_files",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "fileId": "file-001",
                                        "fileName": "merged_001.mp3",
                                        "duration": 186000,
                                        "createTime": 1773038597000,
                                        "fileSize": 1024000,
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_get_audio_download_url",
                        AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync._download_file",
                        AsyncMock(return_value=1024000),
                    ),
                    patch("smart_badge_api.dingtalk_audio_sync.dispatch_dingtalk_recording_pipeline") as dispatch_mock,
                ):
                    result = await sync_dingtalk_audio_files(db, lookback_minutes=60)

                assert result.imported == 1
                assert result.queued == 1
                assert result.failed == 0

                stage_key = _stage_key("SSYX41022508", "file-001")
                paths = _ensure_stage_paths()
                manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
                assert manifest["status"] == "downloaded"
                assert manifest["deviceCode"] == "SSYX41022508"
                assert manifest["staffId"] == staff.id
                assert manifest["staffName"] == "钟露"
                assert manifest["staffRole"] == "consultant"
                assert manifest["stagedFileName"] == "dingtalk_SSYX41022508_file-001.mp3"
                dispatch_mock.assert_called_once_with(stage_key)

                recordings = (await db.execute(select(Recording))).scalars().all()
                assert len(recordings) == 1
                assert recordings[0].file_name == "dingtalk_SSYX41022508_file-001.mp3"
                assert recordings[0].status == "uploaded"
                assert recordings[0].staff_id == staff.id
                assert (await db.execute(select(Transcript))).scalars().all() == []
                assert (await db.execute(select(AnalysisTask))).scalars().all() == []
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_files_uses_iot_for_changsha_yamei(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(
                    name="长沙顾问",
                    external_account="65010001",
                    hospital_code="6501",
                    hospital_short_name="长沙雅美",
                    permission_role="staff",
                    is_active=True,
                )
                db.add(staff)
                await db.flush()
                device = Device(
                    name="长沙工牌",
                    device_code="SSYX51049784",
                    staff_id=staff.id,
                    hospital_code="6501",
                    hospital_short_name="长沙雅美",
                    is_active=True,
                )
                db.add(device)
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.iot_list_audio_files",
                        AsyncMock(
                            return_value=[
                                {
                                    "sn": "SSYX51049784",
                                    "fileId": "iot:event-001",
                                    "fileName": "iot_audio.mp3",
                                    "duration": 186000,
                                    "createTime": 1773038597000,
                                    "fileSize": 1024000,
                                    "downloadUrl": "https://example.com/iot-audio.mp3",
                                    "remoteProvider": "iot",
                                }
                            ]
                        ),
                    ) as iot_list,
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_list_audio_files",
                        AsyncMock(side_effect=AssertionError("Changsha Yamei audio should use IOT")),
                    ) as dvi_list,
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_get_audio_download_url",
                        AsyncMock(side_effect=AssertionError("IOT audio should use direct downloadUrl")),
                    ) as dvi_download,
                    patch(
                        "smart_badge_api.dingtalk_audio_sync._download_file",
                        AsyncMock(return_value=1024000),
                    ) as download_file,
                    patch("smart_badge_api.dingtalk_audio_sync.dispatch_dingtalk_recording_pipeline") as dispatch_mock,
                ):
                    result = await sync_dingtalk_audio_files(db, lookback_minutes=60)

                assert result.imported == 1
                assert result.queued == 1
                assert result.failed == 0
                iot_list.assert_awaited_once()
                dvi_list.assert_not_awaited()
                dvi_download.assert_not_awaited()
                download_file.assert_awaited_once()
                assert download_file.await_args.args[0] == "https://example.com/iot-audio.mp3"

                stage_key = _stage_key("SSYX51049784", "iot:event-001")
                paths = _ensure_stage_paths()
                manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
                assert manifest["remoteProvider"] == "iot"
                assert manifest["deviceCode"] == "SSYX51049784"
                assert manifest["staffId"] == staff.id
                dispatch_mock.assert_called_once_with(stage_key)
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_files_filters_short_audio_before_download_or_asr(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.delenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", raising=False)
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff = Staff(name="钟露", external_account="86000995", permission_role="staff", is_active=True)
                db.add(staff)
                await db.flush()
                device = Device(name="八号工牌", device_code="SSYX41022508", staff_id=staff.id, is_active=True)
                db.add(device)
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_list_audio_files",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "fileId": "file-short",
                                        "fileName": "short.mp3",
                                        "duration": 59000,
                                        "createTime": 1773038597000,
                                        "fileSize": 512000,
                                    }
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_get_audio_download_url",
                        AsyncMock(side_effect=AssertionError("short audio should not fetch download url")),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync._download_file",
                        AsyncMock(side_effect=AssertionError("short audio should not be downloaded")),
                    ),
                    patch("smart_badge_api.dingtalk_audio_sync.dispatch_dingtalk_recording_pipeline") as dispatch_mock,
                    patch("smart_badge_api.dingtalk_audio_sync._sync_visit_orders_for_recording_context", AsyncMock()),
                ):
                    result = await sync_dingtalk_audio_files(db, lookback_minutes=60)

                assert result.imported == 1
                assert result.filtered == 1
                assert result.queued == 0
                assert result.failed == 0
                assert result.items[0].status == "filtered"
                dispatch_mock.assert_not_called()

                stage_key = _stage_key("SSYX41022508", "file-short")
                paths = _ensure_stage_paths()
                manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
                assert manifest["status"] == "filtered"
                assert manifest["qualityStage"] == "pre_asr"
                assert "低于最小时长 60 秒" in manifest["qualityReason"]
                assert manifest["durationSeconds"] == 59
                assert not Path(manifest["audioPath"]).exists()

                recordings = (await db.execute(select(Recording))).scalars().all()
                assert len(recordings) == 1
                assert recordings[0].status == "filtered"
                assert recordings[0].duration_seconds == 59
                assert (await db.execute(select(Transcript))).scalars().all() == []
                assert (await db.execute(select(AnalysisTask))).scalars().all() == []
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_files_uses_historical_badge_owner_by_recording_time(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(name="员工A", external_account="81010001", permission_role="staff", is_active=True)
                staff_b = Staff(name="员工B", external_account="81010002", permission_role="staff", is_active=True)
                db.add_all([staff_a, staff_b])
                await db.commit()
                await db.refresh(staff_a)
                await db.refresh(staff_b)

                await bind_staff_to_device(
                    db,
                    staff=staff_a,
                    device_code="SSYX41022508",
                    device_name="八号工牌",
                    effective_from="2026-04-01T09:00:00+08:00",
                )
                await bind_staff_to_device(
                    db,
                    staff=staff_b,
                    device_code="SSYX41022508",
                    device_name="八号工牌",
                    effective_from="2026-04-10T09:00:00+08:00",
                )

                with (
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_list_audio_files",
                        AsyncMock(
                            return_value={
                                "result": [
                                    {
                                        "fileId": "file-before",
                                        "fileName": "before.mp3",
                                        "duration": 120000,
                                        "createTime": 1775350800000,  # 2026-04-04 01:00:00 UTC
                                        "fileSize": 1000,
                                    },
                                    {
                                        "fileId": "file-after",
                                        "fileName": "after.mp3",
                                        "duration": 120000,
                                        "createTime": 1776042000000,  # 2026-04-12 01:00:00 UTC
                                        "fileSize": 1000,
                                    },
                                ]
                            }
                        ),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync.dvi_get_audio_download_url",
                        AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
                    ),
                    patch(
                        "smart_badge_api.dingtalk_audio_sync._download_file",
                        AsyncMock(return_value=1000),
                    ),
                    patch("smart_badge_api.dingtalk_audio_sync.dispatch_dingtalk_recording_pipeline"),
                ):
                    result = await sync_dingtalk_audio_files(db, lookback_minutes=60)

                assert result.imported == 2

                paths = _ensure_stage_paths()
                before_manifest = json.loads(
                    _manifest_path(paths, _stage_key("SSYX41022508", "file-before")).read_text(encoding="utf-8")
                )
                after_manifest = json.loads(
                    _manifest_path(paths, _stage_key("SSYX41022508", "file-after")).read_text(encoding="utf-8")
                )

                assert before_manifest["staffId"] == staff_a.id
                assert before_manifest["staffName"] == "员工A"
                assert after_manifest["staffId"] == staff_b.id
                assert after_manifest["staffName"] == "员工B"
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_sync_dingtalk_audio_files_skips_when_manifest_exists(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            existing_stage_key = _stage_key("SN001", "dup-file")
            _manifest_path(paths, existing_stage_key).write_text(
                json.dumps({"stageKey": existing_stage_key, "status": "downloaded"}, ensure_ascii=False),
                encoding="utf-8",
            )

            async with session_factory() as db:
                staff = Staff(name="杜娟", external_account="81019369", permission_role="staff", is_active=True)
                db.add(staff)
                await db.flush()
                db.add(Device(name="一号工牌", device_code="SN001", staff_id=staff.id, is_active=True))
                await db.commit()

                with patch(
                    "smart_badge_api.dingtalk_audio_sync.dvi_list_audio_files",
                    AsyncMock(
                        return_value={
                            "result": [
                                {
                                    "fileId": "dup-file",
                                    "fileName": "dup.mp3",
                                    "duration": 62000,
                                    "createTime": 1773038597000,
                                }
                            ]
                        }
                    ),
                ):
                    result = await sync_dingtalk_audio_files(db, lookback_minutes=60)

                assert result.imported == 0
                assert result.skipped == 1
                assert result.items[0].status == "skipped"
                assert "已暂存" in result.items[0].message
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_writes_staged_transcript_and_result(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_path = paths.audio_dir / "SN001"
            audio_path.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_path / "dingtalk_SN001_file-002.mp3"
            raw_audio.write_bytes(b"ID3demo")

            stage_key = _stage_key("SN001", "file-002")
            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SN001",
                "deviceId": "device001",
                "staffId": "staff001",
                "fileId": "file-002",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(raw_audio),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
                "status": "downloaded",
                "createdAt": "2026-04-09T00:00:00+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(
                        return_value=(
                            [
                                {"speaker": "consultant", "text": "您好，我先了解一下您的需求", "begin_ms": 0, "end_ms": 3000},
                                {"speaker": "customer", "text": "我主要想改善法令纹", "begin_ms": 3200, "end_ms": 6000},
                            ],
                            "您好，我先了解一下您的需求 我主要想改善法令纹",
                            6000,
                        )
                    ),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value=_analysis_result_with_indication(88),
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "analyzed"
            assert Path(updated_manifest["transcriptPath"]).is_file()
            assert Path(updated_manifest["analysisInputPath"]).is_file()
            assert Path(updated_manifest["analysisResultPath"]).is_file()

            transcript_doc = json.loads(Path(updated_manifest["transcriptPath"]).read_text(encoding="utf-8"))
            assert transcript_doc["stageKey"] == stage_key
            assert len(transcript_doc["utterances"]) == 2

            analysis_input = json.loads(Path(updated_manifest["analysisInputPath"]).read_text(encoding="utf-8"))
            assert analysis_input["payload"]["transcribeResult"][0]["role"] == "badge_owner"
            assert analysis_input["payload"]["transcribeResult"][0]["speaker_label"] == "工牌本人"
            assert analysis_input["payload"]["transcribeResult"][1]["speaker_label"] == "主客户"

            result_doc = json.loads(Path(updated_manifest["analysisResultPath"]).read_text(encoding="utf-8"))
            assert result_doc["consultation_evaluation"]["overall_score"] == 88

            async with session_factory() as db:
                recording = (await db.execute(select(Recording))).scalars().one()
                transcript = (await db.execute(select(Transcript))).scalars().one()
                task = (await db.execute(select(AnalysisTask))).scalars().one()

                assert recording.status == "analyzed"
                assert recording.file_name == raw_audio.name
                assert transcript.recording_id == recording.id
                assert transcript.status == "completed"
                assert transcript.full_text == "您好，我先了解一下您的需求 我主要想改善法令纹"
                assert task.file_name == f"recording_{recording.id}.json"
                assert task.status == "done"
                assert task.overall_score == 3.33
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_resolves_legacy_audio_path(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_dir = paths.audio_dir / "SNLEGACY"
            audio_dir.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_dir / "dingtalk_SNLEGACY_file-legacy.mp3"
            raw_audio.write_bytes(b"ID3demo")
            legacy_audio_path = Path("/app/uploads") / raw_audio.relative_to(get_settings().upload_path)

            stage_key = _stage_key("SNLEGACY", "file-legacy")
            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SNLEGACY",
                "deviceId": "device-legacy",
                "staffId": "staff-legacy",
                "fileId": "file-legacy",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(legacy_audio_path),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-13T00:00:00+00:00",
                "status": "downloaded",
                "createdAt": "2026-04-13T00:00:00+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(
                        return_value=(
                            [
                                {"speaker": "consultant", "text": "您好，我先了解一下您的需求", "begin_ms": 0, "end_ms": 3000},
                                {"speaker": "customer", "text": "我主要想改善法令纹", "begin_ms": 3200, "end_ms": 6000},
                            ],
                            "您好，我先了解一下您的需求 我主要想改善法令纹",
                            6000,
                        )
                    ),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value=_analysis_result_with_indication(88),
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "analyzed"
            assert updated_manifest["audioPath"] == str(raw_audio)

            async with session_factory() as db:
                recording = (await db.execute(select(Recording))).scalars().one()
                assert recording.file_path == str(raw_audio.relative_to(get_settings().upload_path))
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_reuses_existing_transcript_for_analysis_retry(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_dir = paths.audio_dir / "SNRETRY"
            audio_dir.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_dir / "dingtalk_SNRETRY_file-retry.mp3"
            raw_audio.write_bytes(b"ID3demo")

            stage_key = _stage_key("SNRETRY", "file-retry")
            transcript_path = paths.transcript_dir / f"{stage_key}.transcript.json"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(
                json.dumps(
                    {
                        "stageKey": stage_key,
                        "deviceCode": "SNRETRY",
                        "fileId": "file-retry",
                        "audioPath": str(raw_audio),
                        "asrProvider": "tencent_asr",
                        "durationMs": 6500,
                        "fullText": "您好，我想改善法令纹 我比较担心恢复期",
                        "utterances": [
                            {"speaker": "consultant", "text": "您好，我想先了解您的需求", "begin_ms": 0, "end_ms": 2800},
                            {"speaker": "customer", "text": "我想改善法令纹，我比较担心恢复期", "begin_ms": 3000, "end_ms": 6500},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SNRETRY",
                "deviceId": "device-retry",
                "staffId": "staff-retry",
                "fileId": "file-retry",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(raw_audio),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
                "status": "failed",
                "errorMessage": "Expecting value: line 1 column 1 (char 0)",
                "transcriptPath": str(transcript_path),
                "createdAt": "2026-04-09T00:00:00+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(side_effect=AssertionError("should not call ASR when transcript already exists")),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value=_analysis_result_with_indication(86),
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "analyzed"
            assert updated_manifest["transcriptPath"] == str(transcript_path)
            assert Path(updated_manifest["analysisInputPath"]).is_file()
            assert Path(updated_manifest["analysisResultPath"]).is_file()
            assert "errorMessage" not in updated_manifest

            async with session_factory() as db:
                recording = (await db.execute(select(Recording))).scalars().one()
                transcript = (await db.execute(select(Transcript))).scalars().one()
                task = (await db.execute(select(AnalysisTask))).scalars().one()

                assert recording.status == "analyzed"
                assert transcript.status == "completed"
                assert transcript.asr_provider == "tencent_asr"
                assert task.status == "done"
                assert task.overall_score == 3.33
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_non_dialogue_transcript(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN002"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN002_file-003.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN002", "file-003")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN002",
            "deviceId": "device002",
            "staffId": "staff002",
            "fileId": "file-003",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": 180000,
            "durationSeconds": 180,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                AsyncMock(
                    return_value=(
                        [
                            {"speaker": "consultant", "text": "今天复盘一下排班", "begin_ms": 0, "end_ms": 5000},
                            {"speaker": "consultant", "text": "再确认一下内部流程", "begin_ms": 5200, "end_ms": 11000},
                        ],
                        "今天复盘一下排班 再确认一下内部流程",
                        11000,
                    )
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "post_asr"
            assert "双人沟通" in updated_manifest["qualityReason"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_too_long_audio(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MAX_DURATION_SECONDS", "3600")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN003"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN003_file-004.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN003", "file-004")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN003",
            "deviceId": "device003",
            "staffId": "staff003",
            "fileId": "file-004",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": 7201000,
            "durationSeconds": 7201,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                AsyncMock(side_effect=AssertionError("too long audio should be filtered before ASR")),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "pre_asr"
            assert "超过最长时长" in updated_manifest["qualityReason"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_too_short_audio_even_when_duration_missing(
    monkeypatch,
    tmp_path,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN003A"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN003A_file-004a.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN003A", "file-004a")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN003A",
            "deviceId": "device003a",
            "staffId": "staff003a",
            "fileId": "file-004a",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": None,
            "durationSeconds": None,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with (
                patch(
                    "smart_badge_api.dingtalk_audio_sync._probe_audio_duration_seconds",
                    return_value=12,
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(side_effect=AssertionError("too short audio should be filtered before Tencent ASR")),
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "pre_asr"
            assert "低于最小时长" in updated_manifest["qualityReason"]
            assert updated_manifest["durationSeconds"] == 12
            assert updated_manifest["durationMs"] == 12000
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_without_customer_role(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MAX_DURATION_SECONDS", "7200")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN004"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN004_file-005.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN004", "file-005")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN004",
            "deviceId": "device004",
            "staffId": "staff004",
            "fileId": "file-005",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": 180000,
            "durationSeconds": 180,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                AsyncMock(
                    return_value=(
                        [
                            {"speaker": "consultant", "text": "我们看一下今天的排班和接待安排", "begin_ms": 0, "end_ms": 4000},
                            {"speaker": "doctor", "text": "好的，内部流程我再确认一遍", "begin_ms": 4200, "end_ms": 8000},
                        ],
                        "我们看一下今天的排班和接待安排 好的，内部流程我再确认一遍",
                        8000,
                    )
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "post_asr"
            assert "客户与接诊人员" in updated_manifest["qualityReason"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_allows_single_speaker_consultation_with_strong_dialogue_signal(
    monkeypatch,
    tmp_path,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MAX_DURATION_SECONDS", "7200")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "4")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "40")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_INTERNAL_KEYWORD_THRESHOLD", "2")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_path = paths.audio_dir / "SN004A"
            audio_path.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_path / "dingtalk_SN004A_file-005a.mp3"
            raw_audio.write_bytes(b"ID3demo")

            stage_key = _stage_key("SN004A", "file-005a")
            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SN004A",
                "deviceId": "device004a",
                "staffId": "staff004a",
                "fileId": "file-005a",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(raw_audio),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
                "status": "downloaded",
                "createdAt": "2026-04-09T00:00:00+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            utterances = [
                {"speaker": "consultant", "text": "您好，我先了解一下今天的诉求。", "begin_ms": 0, "end_ms": 1200},
                {"speaker": "consultant", "text": "我主要想改善皮肤状态和毛孔。", "begin_ms": 1400, "end_ms": 2600},
                {"speaker": "consultant", "text": "可以，我们先看适合做什么项目。", "begin_ms": 2800, "end_ms": 4200},
                {"speaker": "consultant", "text": "价格和恢复期大概是怎样？", "begin_ms": 4400, "end_ms": 5600},
                {"speaker": "consultant", "text": "如果想做水光和光子，都可以面诊后定。", "begin_ms": 5800, "end_ms": 7600},
                {"speaker": "consultant", "text": "我更关注效果和预算。", "begin_ms": 7800, "end_ms": 9000},
                {"speaker": "consultant", "text": "好的，那我把两个项目方案给您讲一下。", "begin_ms": 9200, "end_ms": 11000},
                {"speaker": "consultant", "text": "嗯，可以。", "begin_ms": 11200, "end_ms": 12000},
            ]
            full_text = " ".join(item["text"] for item in utterances)

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(return_value=(utterances, full_text, 12000)),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value=_analysis_result_with_indication(85),
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "analyzed"
            assert "qualityReason" not in updated_manifest
            assert Path(updated_manifest["analysisResultPath"]).is_file()
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_internal_two_speaker_transcript(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MAX_DURATION_SECONDS", "7200")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_INTERNAL_KEYWORD_THRESHOLD", "2")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN005"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN005_file-006.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN005", "file-006")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN005",
            "deviceId": "device005",
            "staffId": "staff005",
            "fileId": "file-006",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": 180000,
            "durationSeconds": 180,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                AsyncMock(
                    return_value=(
                        [
                            {"speaker": "SPEAKER_00", "text": "今天先复盘一下排班安排", "begin_ms": 0, "end_ms": 4000},
                            {"speaker": "SPEAKER_01", "text": "好的，我再把内部流程和培训计划确认一下", "begin_ms": 4200, "end_ms": 9000},
                        ],
                        "今天先复盘一下排班安排 好的，我再把内部流程和培训计划确认一下",
                        9000,
                    )
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "post_asr"
            assert "内部沟通关键词" in updated_manifest["qualityReason"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_analysis_without_indications(
    monkeypatch,
    tmp_path,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "60")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_path = paths.audio_dir / "SN004B"
            audio_path.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_path / "dingtalk_SN004B_file-005b.mp3"
            raw_audio.write_bytes(b"ID3demo")

            stage_key = _stage_key("SN004B", "file-005b")
            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SN004B",
                "deviceId": "device004b",
                "staffId": "staff004b",
                "fileId": "file-005b",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(raw_audio),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
                "status": "downloaded",
                "createdAt": "2026-04-09T00:00:00+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            utterances = [
                {"speaker": "consultant", "text": "您好，我先了解一下今天的诉求。", "begin_ms": 0, "end_ms": 4000},
                {"speaker": "customer", "text": "我今天就是随便聊一下，没有医美项目。", "begin_ms": 4200, "end_ms": 9000},
            ]
            full_text = " ".join(item["text"] for item in utterances)

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(return_value=(utterances, full_text, 9000)),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value={
                        "standardized_indications": {"summary": "对话中未识别出可标准化的适应症", "items": []},
                        "consultation_evaluation": {"overall_score": 0},
                    },
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "post_analysis"
            assert "医美适应症" in updated_manifest["qualityReason"]
            assert Path(updated_manifest["analysisResultPath"]).is_file()

            async with session_factory() as db:
                recording = (await db.execute(select(Recording))).scalars().one()
                assert recording.status == "filtered"
                assert (await db.execute(select(AnalysisTask))).scalars().all() == []
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_repairs_confirmed_eye_bag_consult_before_quality_filter(
    monkeypatch,
    tmp_path,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "60")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        get_settings.cache_clear()

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            paths = _ensure_stage_paths()
            audio_path = paths.audio_dir / "SN004C"
            audio_path.mkdir(parents=True, exist_ok=True)
            raw_audio = audio_path / "dingtalk_SN004C_file-005c.mp3"
            raw_audio.write_bytes(b"ID3demo")

            stage_key = _stage_key("SN004C", "file-005c")
            manifest = {
                "stageKey": stage_key,
                "deviceCode": "SN004C",
                "deviceId": "device004c",
                "staffId": "staff004c",
                "fileId": "file-005c",
                "remoteFileName": "origin.mp3",
                "stagedFileName": raw_audio.name,
                "audioPath": str(raw_audio),
                "fileSize": 7,
                "durationMs": 180000,
                "durationSeconds": 180,
                "remoteCreatedAt": "2026-04-30T01:12:28+00:00",
                "status": "downloaded",
                "createdAt": "2026-04-30T01:12:28+00:00",
            }
            _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            utterances = [
                {
                    "speaker": "consultant",
                    "text": "我是今天接待你们的美学顾问张鑫。",
                    "begin_ms": 0,
                    "end_ms": 3000,
                },
                {
                    "speaker": "consultant",
                    "text": "你们是想看眼袋是吗？",
                    "begin_ms": 15000,
                    "end_ms": 17000,
                },
                {"speaker": "customer", "text": "嗯嗯。", "begin_ms": 19000, "end_ms": 20000},
            ]
            full_text = " ".join(item["text"] for item in utterances)

            with (
                patch("smart_badge_api.dingtalk_audio_sync._session_factory", session_factory),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                    AsyncMock(return_value=(utterances, full_text, 180000)),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync.build_system_prompt",
                    AsyncMock(return_value="system-prompt"),
                ),
                patch(
                    "smart_badge_api.dingtalk_audio_sync._run_analysis_sync",
                    return_value={
                        "customer_primary_demands": {"summary": "", "items": []},
                        "standardized_indications": {
                            "summary": "对话中未识别出可标准化的适应症",
                            "items": [],
                        },
                        "consultation_evaluation": {"overall_score": 0},
                    },
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "analyzed"
            assert "qualityStage" not in updated_manifest

            result_doc = json.loads(Path(updated_manifest["analysisResultPath"]).read_text(encoding="utf-8"))
            primary_items = result_doc["customer_primary_demands"]["items"]
            indication_items = result_doc["standardized_indications"]["items"]
            assert primary_items[0]["demand"] == "咨询眼袋方案"
            assert indication_items[0]["indication_name"] == "眼袋"
            assert "嗯嗯" in indication_items[0]["evidence"]

            async with session_factory() as db:
                recording = (await db.execute(select(Recording))).scalars().one()
                task = (await db.execute(select(AnalysisTask))).scalars().one()
                assert recording.status == "analyzed"
                assert task.status == "done"
        finally:
            get_settings.cache_clear()
            await engine.dispose()

    asyncio.run(scenario())


def test_execute_dingtalk_recording_pipeline_filters_internal_transcript_even_with_semantic_roles(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", "20")
        monkeypatch.setenv("DINGTALK_AUDIO_MAX_DURATION_SECONDS", "7200")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_UTTERANCE_COUNT", "2")
        monkeypatch.setenv("DINGTALK_AUDIO_MIN_TRANSCRIPT_CHARS", "10")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_MULTI_SPEAKER", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_REQUIRE_CUSTOMER_ROLE", "true")
        monkeypatch.setenv("DINGTALK_AUDIO_INTERNAL_KEYWORD_THRESHOLD", "2")
        get_settings.cache_clear()

        paths = _ensure_stage_paths()
        audio_path = paths.audio_dir / "SN006"
        audio_path.mkdir(parents=True, exist_ok=True)
        raw_audio = audio_path / "dingtalk_SN006_file-007.mp3"
        raw_audio.write_bytes(b"ID3demo")

        stage_key = _stage_key("SN006", "file-007")
        manifest = {
            "stageKey": stage_key,
            "deviceCode": "SN006",
            "deviceId": "device006",
            "staffId": "staff006",
            "staffName": "顾问A",
            "staffRole": "consultant",
            "fileId": "file-007",
            "remoteFileName": "origin.mp3",
            "stagedFileName": raw_audio.name,
            "audioPath": str(raw_audio),
            "fileSize": 7,
            "durationMs": 180000,
            "durationSeconds": 180,
            "remoteCreatedAt": "2026-04-09T00:00:00+00:00",
            "status": "downloaded",
            "createdAt": "2026-04-09T00:00:00+00:00",
        }
        _manifest_path(paths, stage_key).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_sync.transcribe_audio_file",
                AsyncMock(
                    return_value=(
                        [
                            {"speaker": "consultant", "text": "今天先复盘一下排班安排", "begin_ms": 0, "end_ms": 4000},
                            {"speaker": "customer", "text": "好的，我再把内部流程和培训计划确认一下", "begin_ms": 4200, "end_ms": 9000},
                        ],
                        "今天先复盘一下排班安排 好的，我再把内部流程和培训计划确认一下",
                        9000,
                    )
                ),
            ):
                await execute_dingtalk_recording_pipeline(stage_key)

            updated_manifest = json.loads(_manifest_path(paths, stage_key).read_text(encoding="utf-8"))
            assert updated_manifest["status"] == "filtered"
            assert updated_manifest["qualityStage"] == "post_asr"
            assert "内部沟通关键词" in updated_manifest["qualityReason"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())
