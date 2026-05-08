from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from smart_badge_api.core.config import get_settings
from smart_badge_api.periodic_locks import ASR_HOTWORD_SYNC_LOCK_ID, periodic_advisory_lock

logger = logging.getLogger("smart_badge.asr_hotword_sync")


def _scan_script_path() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "scan_all_archive_terms.py"


def _scan_report_path() -> Path:
    return get_settings().asr_runtime_path / "full_archive_term_scan.json"


async def run_asr_hotword_sync_once() -> dict[str, Any]:
    settings = get_settings()
    script_path = _scan_script_path()
    report_path = _scan_report_path()
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[2])
    current_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = f"{src_path}:{current_pythonpath}" if current_pythonpath else src_path

    cmd = [
        sys.executable,
        str(script_path),
        "--stage-root",
        str(settings.dingtalk_audio_stage_path),
        "--output",
        str(report_path),
        "--sync-hotwords",
    ]

    logger.info("starting ASR hotword sync scan script=%s output=%s", script_path, report_path)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=max(settings.asr_hotword_sync_timeout_seconds, 1),
        )
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.terminate()
        with suppress(asyncio.CancelledError):
            await process.wait()
        raise
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.terminate()
        with suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=10)
        raise RuntimeError(
            f"ASR hotword sync timed out after {settings.asr_hotword_sync_timeout_seconds} seconds"
        )

    output = stdout_bytes.decode("utf-8", errors="replace").strip()
    if len(output) > 4000:
        output = f"{output[:4000]}...<truncated>"
    if process.returncode != 0:
        raise RuntimeError(f"ASR hotword sync failed with exit code {process.returncode}: {output}")

    summary: dict[str, Any] = {
        "report_path": str(report_path),
        "output": output,
    }
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            summary["report_summary"] = report.get("summary")
            summary["auto_hotword_sync"] = report.get("auto_hotword_sync")
        except Exception:
            logger.exception("failed to parse ASR hotword sync report: %s", report_path)
    return summary


async def periodic_asr_hotword_sync(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int | None = None,
) -> None:
    settings = get_settings()
    resolved_interval = (
        interval_seconds if interval_seconds is not None else settings.asr_hotword_auto_sync_interval_seconds
    )
    logger.info(
        "starting ASR hotword sync loop interval_seconds=%d report_path=%s",
        resolved_interval,
        _scan_report_path(),
    )

    while not stop_event.is_set():
        try:
            async with periodic_advisory_lock("asr_hotword_sync", ASR_HOTWORD_SYNC_LOCK_ID) as acquired:
                if acquired:
                    result = await run_asr_hotword_sync_once()
                    logger.info(
                        "ASR hotword sync finished summary=%s auto_hotword_sync=%s",
                        result.get("report_summary"),
                        result.get("auto_hotword_sync"),
                    )
        except Exception as exc:
            logger.exception("ASR hotword sync failed: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(resolved_interval, 1))
        except asyncio.TimeoutError:
            continue

    logger.info("ASR hotword sync loop stopped")
