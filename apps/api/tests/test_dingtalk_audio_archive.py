from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from smart_badge_api.core.config import get_settings
from smart_badge_api.dingtalk_audio_archive import (
    RemoteAudioItem,
    archive_audio_item,
    compute_incremental_archive_window,
    get_archive_root,
    list_all_audio_for_device,
)

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, content: bytes = b"ID3demo") -> None:
        self.content = content
        self.calls: list[str] = []

    async def get(self, url: str) -> _FakeResponse:
        self.calls.append(url)
        return _FakeResponse(self.content)


def _timestamp_ms(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=TZ_SHANGHAI).timestamp() * 1000)


def test_archive_audio_item_writes_month_day_named_file_and_sidecar(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        item = RemoteAudioItem(
            sn="SN001",
            file_id="file-001",
            file_name="origin.mp3",
            duration_ms=62000,
            file_size=1024,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
        )
        client = _FakeClient()

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
            ):
                result = await archive_audio_item(item, client=client, archive_root=get_archive_root())

            expected_audio = tmp_path / "uploads" / "dingtalk_pending" / "archive" / "SN001" / "202603" / "0309_145601.mp3"
            expected_meta = expected_audio.with_suffix(".json")

            assert result.status == "downloaded"
            assert result.saved_path == expected_audio
            assert expected_audio.read_bytes() == b"ID3demo"
            assert client.calls == ["https://example.com/audio.mp3"]

            payload = json.loads(expected_meta.read_text(encoding="utf-8"))
            assert payload["fileId"] == "file-001"
            assert payload["audioPath"] == str(expected_audio)
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_archive_audio_item_uses_iot_direct_download_url(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        item = RemoteAudioItem(
            sn="SSYX51049784",
            file_id="iot:event-001",
            file_name="origin.mp3",
            duration_ms=62000,
            file_size=1024,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
            download_url="https://example.com/iot-audio.mp3",
            source="iot",
        )
        client = _FakeClient()

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(side_effect=AssertionError("IOT audio should not call DVI download API")),
            ) as dvi_download:
                result = await archive_audio_item(item, client=client, archive_root=get_archive_root())

            expected_audio = (
                tmp_path
                / "uploads"
                / "dingtalk_pending"
                / "archive"
                / "SSYX51049784"
                / "202603"
                / "0309_145601.mp3"
            )
            expected_meta = expected_audio.with_suffix(".json")

            assert result.status == "downloaded"
            assert result.saved_path == expected_audio
            assert client.calls == ["https://example.com/iot-audio.mp3"]
            dvi_download.assert_not_awaited()

            payload = json.loads(expected_meta.read_text(encoding="utf-8"))
            assert payload["fileId"] == "iot:event-001"
            assert payload["source"] == "iot"
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_archive_audio_item_filters_short_audio_without_download(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        monkeypatch.delenv("DINGTALK_AUDIO_MIN_DURATION_SECONDS", raising=False)
        get_settings.cache_clear()

        item = RemoteAudioItem(
            sn="SN001",
            file_id="file-short",
            file_name="origin.mp3",
            duration_ms=59000,
            file_size=512,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
        )
        client = _FakeClient()

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(side_effect=AssertionError("short archive audio should not fetch download url")),
            ):
                result = await archive_audio_item(item, client=client, archive_root=get_archive_root())

            expected_audio = tmp_path / "uploads" / "dingtalk_pending" / "archive" / "SN001" / "202603" / "0309_145601.mp3"
            expected_meta = expected_audio.with_suffix(".json")

            assert result.status == "filtered"
            assert result.saved_path is None
            assert client.calls == []
            assert not expected_audio.exists()

            payload = json.loads(expected_meta.read_text(encoding="utf-8"))
            assert payload["fileId"] == "file-short"
            assert payload["status"] == "filtered"
            assert payload["qualityStage"] == "pre_asr"
            assert "低于最小时长 60 秒" in payload["qualityReason"]
            assert payload["audioPath"] == str(expected_audio)
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_archive_audio_item_skips_existing_sidecar_with_same_file_id(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        item = RemoteAudioItem(
            sn="SN001",
            file_id="file-001",
            file_name="origin.mp3",
            duration_ms=62000,
            file_size=1024,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
        )

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
            ):
                first = await archive_audio_item(item, client=_FakeClient(), archive_root=get_archive_root())

            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(side_effect=AssertionError("should not fetch download url for archived file")),
            ):
                second = await archive_audio_item(item, client=_FakeClient(), archive_root=get_archive_root())

            assert first.status == "downloaded"
            assert second.status == "skipped"
            assert second.saved_path == first.saved_path
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_archive_audio_item_redownloads_when_existing_audio_missing(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN001" / "202603"
        archive_dir.mkdir(parents=True, exist_ok=True)
        missing_audio = archive_dir / "0309_145601.mp3"
        missing_audio.with_suffix(".json").write_text(
            json.dumps(
                {
                    "fileId": "file-001",
                    "audioPath": str(missing_audio),
                    "status": "downloaded",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        item = RemoteAudioItem(
            sn="SN001",
            file_id="file-001",
            file_name="origin.mp3",
            duration_ms=62000,
            file_size=1024,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
        )
        client = _FakeClient(content=b"ID3-redownload")

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
            ):
                result = await archive_audio_item(item, client=client, archive_root=archive_root)

            assert result.status == "downloaded"
            assert result.saved_path == missing_audio
            assert missing_audio.read_bytes() == b"ID3-redownload"
            assert client.calls == ["https://example.com/audio.mp3"]
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_archive_audio_item_adds_numeric_suffix_when_same_second_conflicts(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setenv("DINGTALK_AUDIO_STAGE_DIR", "dingtalk_pending")
        get_settings.cache_clear()

        archive_root = get_archive_root()
        archive_dir = archive_root / "SN001" / "202603"
        archive_dir.mkdir(parents=True, exist_ok=True)
        existing_audio = archive_dir / "0309_145601.mp3"
        existing_meta = archive_dir / "0309_145601.json"
        existing_audio.write_bytes(b"old")
        existing_meta.write_text(
            json.dumps({"fileId": "old-file", "audioPath": str(existing_audio)}, ensure_ascii=False),
            encoding="utf-8",
        )

        item = RemoteAudioItem(
            sn="SN001",
            file_id="new-file",
            file_name="origin.mp3",
            duration_ms=62000,
            file_size=1024,
            create_time_ms=_timestamp_ms(2026, 3, 9, 14, 56, 1),
        )

        try:
            with patch(
                "smart_badge_api.dingtalk_audio_archive.dvi_get_audio_download_url",
                AsyncMock(return_value={"result": {"url": "https://example.com/audio.mp3"}}),
            ):
                result = await archive_audio_item(item, client=_FakeClient(), archive_root=archive_root)

            assert result.status == "downloaded"
            assert result.saved_path == archive_dir / "0309_145601_2.mp3"
            assert result.saved_path.is_file()
            assert result.saved_path.with_suffix(".json").is_file()
        finally:
            get_settings.cache_clear()

    asyncio.run(scenario())


def test_compute_incremental_archive_window_uses_previous_checkpoint_overlap() -> None:
    now = datetime(2026, 3, 10, 10, 0, 0, tzinfo=ZoneInfo("UTC"))
    previous_end = int(datetime(2026, 3, 10, 8, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    start_ms, end_ms = compute_incremental_archive_window(
        now,
        lookback_minutes=60,
        state={"incremental": {"lastWindowEndMs": previous_end}},
    )

    assert end_ms == int(now.timestamp() * 1000)
    assert start_ms == previous_end - 60 * 60 * 1000


def test_list_all_audio_for_device_passes_time_window() -> None:
    async def scenario() -> None:
        with patch(
            "smart_badge_api.dingtalk_audio_archive.dvi_list_audio_files",
            AsyncMock(
                return_value={
                    "result": [
                        {
                            "fileId": "file-001",
                            "fileName": "origin.mp3",
                            "duration": 62000,
                            "fileSize": 1024,
                            "createTime": _timestamp_ms(2026, 3, 9, 14, 56, 1),
                        }
                    ],
                    "nextToken": "",
                }
            ),
        ) as mocked:
            items = await list_all_audio_for_device(
                "SN001",
                start_timestamp=111,
                end_timestamp=222,
            )

        assert len(items) == 1
        assert items[0].file_id == "file-001"
        mocked.assert_awaited_once_with(
            "SN001",
            max_results=get_settings().dingtalk_audio_sync_page_size,
            next_token="",
            start_timestamp=111,
            end_timestamp=222,
        )

    asyncio.run(scenario())
