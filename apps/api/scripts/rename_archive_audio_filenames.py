from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import Recording
from smart_badge_api.db.session import _session_factory
from smart_badge_api.dingtalk_audio_archive import SYNC_STATE_FILE_NAME, get_archive_root

OLD_ARCHIVE_FILE_RE = re.compile(
    r"^(?P<day>\d{2})_(?P<hms>\d{6})(?P<index>_\d+)?(?P<ext>\.[A-Za-z0-9]+)$"
)
MONTH_DIR_RE = re.compile(r"^\d{6}$")


@dataclass(slots=True)
class RenamePlan:
    old_audio_path: Path
    new_audio_path: Path
    old_meta_path: Path
    new_meta_path: Path
    old_relative_path: str
    new_relative_path: str
    old_file_name: str
    new_file_name: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rename archived DingTalk audio files from DD_HHMMSS to MMDD_HHMMSS.")
    parser.add_argument("--apply", action="store_true", help="Persist changes. Without this flag, run in dry-run mode.")
    return parser.parse_args()


def _build_new_archive_name(path: Path) -> str | None:
    matched = OLD_ARCHIVE_FILE_RE.match(path.name)
    if not matched:
        return None
    month_folder = path.parent.name.strip()
    if not MONTH_DIR_RE.fullmatch(month_folder):
        return None
    month = month_folder[-2:]
    day = matched.group("day")
    hms = matched.group("hms")
    index = matched.group("index") or ""
    ext = matched.group("ext").lower()
    return f"{month}{day}_{hms}{index}{ext}"


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _build_rename_plans() -> list[RenamePlan]:
    settings = get_settings()
    archive_root = get_archive_root()
    plans: list[RenamePlan] = []

    for meta_path in sorted(archive_root.rglob("*.json")):
        if meta_path.name == SYNC_STATE_FILE_NAME:
            continue
        payload = _read_json(meta_path)
        if payload is None:
            continue
        audio_path_text = str(payload.get("audioPath") or "").strip()
        if not audio_path_text:
            continue
        audio_path = Path(audio_path_text)
        if not audio_path.exists():
            continue
        new_name = _build_new_archive_name(audio_path)
        if not new_name or new_name == audio_path.name:
            continue

        new_audio_path = audio_path.with_name(new_name)
        new_meta_path = meta_path.with_name(f"{Path(new_name).stem}.json")

        if new_audio_path.exists() and new_audio_path != audio_path:
            raise RuntimeError(f"target audio already exists: {new_audio_path}")
        if new_meta_path.exists() and new_meta_path != meta_path:
            raise RuntimeError(f"target metadata already exists: {new_meta_path}")

        plans.append(
            RenamePlan(
                old_audio_path=audio_path,
                new_audio_path=new_audio_path,
                old_meta_path=meta_path,
                new_meta_path=new_meta_path,
                old_relative_path=settings.make_relative_path(audio_path),
                new_relative_path=settings.make_relative_path(new_audio_path),
                old_file_name=audio_path.name,
                new_file_name=new_name,
            )
        )

    return plans


def _apply_filesystem_changes(plans: list[RenamePlan]) -> tuple[int, int]:
    renamed_audio = 0
    renamed_metadata = 0

    for plan in plans:
        payload = _read_json(plan.old_meta_path)
        if payload is None:
            raise RuntimeError(f"invalid metadata json: {plan.old_meta_path}")

        plan.old_audio_path.rename(plan.new_audio_path)
        renamed_audio += 1

        try:
            if plan.old_meta_path != plan.new_meta_path:
                plan.old_meta_path.rename(plan.new_meta_path)
            payload["audioPath"] = str(plan.new_audio_path)
            plan.new_meta_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            renamed_metadata += 1
        except Exception:
            if plan.new_meta_path.exists() and plan.new_meta_path != plan.old_meta_path and not plan.old_meta_path.exists():
                plan.new_meta_path.rename(plan.old_meta_path)
            if plan.new_audio_path.exists() and not plan.old_audio_path.exists():
                plan.new_audio_path.rename(plan.old_audio_path)
            raise

    return renamed_audio, renamed_metadata


def _revert_filesystem_changes(plans: list[RenamePlan]) -> None:
    for plan in reversed(plans):
        if plan.new_meta_path.exists() and plan.new_meta_path != plan.old_meta_path:
            payload = _read_json(plan.new_meta_path) or {}
            payload["audioPath"] = str(plan.old_audio_path)
            plan.new_meta_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            plan.new_meta_path.rename(plan.old_meta_path)
        elif plan.old_meta_path.exists():
            payload = _read_json(plan.old_meta_path) or {}
            payload["audioPath"] = str(plan.old_audio_path)
            plan.old_meta_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if plan.new_audio_path.exists() and not plan.old_audio_path.exists():
            plan.new_audio_path.rename(plan.old_audio_path)


async def _update_recordings(plans: list[RenamePlan], *, apply_changes: bool) -> tuple[int, list[tuple[str, str, str]]]:
    settings = get_settings()
    by_relative_path = {plan.old_relative_path: plan for plan in plans}
    by_absolute_path = {str(plan.old_audio_path): plan for plan in plans}
    updated_rows: list[tuple[str, str, str]] = []

    async with _session_factory() as db:
        rows = (
            await db.execute(
                select(Recording).where(
                    or_(
                        Recording.file_path.like("dingtalk_staging/archive/%"),
                        Recording.file_path.like("%/dingtalk_staging/archive/%"),
                    )
                )
            )
        ).scalars().all()

        for recording in rows:
            plan = by_relative_path.get(recording.file_path) or by_absolute_path.get(recording.file_path)
            if plan is None:
                resolved_path = settings.resolve_file_path(recording.file_path)
                plan = by_absolute_path.get(str(resolved_path))
                if plan is None:
                    continue

            original_path = recording.file_path
            recording.file_path = (
                str(plan.new_audio_path)
                if Path(original_path).is_absolute()
                else plan.new_relative_path
            )
            if recording.file_name == plan.old_file_name:
                recording.file_name = plan.new_file_name
            updated_rows.append((recording.id, plan.old_file_name, plan.new_file_name))

        if apply_changes:
            await db.commit()
        else:
            await db.rollback()

    return len(updated_rows), updated_rows


async def _main() -> None:
    args = _parse_args()
    plans = _build_rename_plans()
    print(f"planned_archive_file_renames={len(plans)}")
    for plan in plans[:20]:
        print(f"{plan.old_audio_path.name} -> {plan.new_audio_path.name}")

    updated_count, updated_rows = await _update_recordings(plans, apply_changes=False)
    print(f"planned_recording_updates={updated_count}")
    for recording_id, old_name, new_name in updated_rows[:20]:
        print(f"{recording_id}: {old_name} -> {new_name}")

    if not args.apply:
        print("dry_run_only=1")
        return

    try:
        renamed_audio, renamed_metadata = _apply_filesystem_changes(plans)
        updated_count, _ = await _update_recordings(plans, apply_changes=True)
    except Exception:
        _revert_filesystem_changes(plans)
        raise

    print(f"renamed_audio_files={renamed_audio}")
    print(f"renamed_metadata_files={renamed_metadata}")
    print(f"updated_recordings={updated_count}")


if __name__ == "__main__":
    asyncio.run(_main())
