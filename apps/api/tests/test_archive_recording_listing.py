import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes import dingtalk as dingtalk_routes
from smart_badge_api.api.routes.recordings import list_archive_recordings
from smart_badge_api.api.routes.dingtalk import (
    _archive_recording_id,
    _build_archive_analysis_summary,
    _build_archive_recording_summary,
    _build_staged_archive_recording_summary,
    _load_archive_recording_index,
    _resolve_archive_analysis_result,
)
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import Staff, StaffManagementRelation, User


def _archive_item(
    item_id: str,
    *,
    create_time: str,
    pipeline_status: str,
    needs_visit_link: bool,
    has_visit_link: bool,
    staff_id: str | None = None,
    staff_hospital_code: str | None = None,
    device_hospital_code: str | None = None,
) -> dict[str, object]:
    return {
        "id": item_id,
        "file_id": f"file-{item_id}",
        "display_file_name": f"{item_id}.mp3",
        "create_time": create_time,
        "pipeline_status": pipeline_status,
        "needs_visit_link": needs_visit_link,
        "has_visit_link": has_visit_link,
        "linked_visit_ids": [],
        "linked_visit_order_refs": [],
        "linked_customer_names": [],
        "has_transcript": True,
        "has_analysis": pipeline_status == "analyzed",
        "staff_id": staff_id,
        "staff_hospital_code": staff_hospital_code,
        "device_hospital_code": device_hospital_code,
    }


def test_list_archive_recordings_prioritizes_pending_and_valid_items() -> None:
    async def scenario() -> None:
        archive_index = {
            "linked-newest": {
                "summary": _archive_item(
                    "linked-newest",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "needs-link-older": {
                "summary": _archive_item(
                    "needs-link-older",
                    create_time="2026-04-14T10:00:00+08:00",
                    pipeline_status="transcribed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "needs-link-newer": {
                "summary": _archive_item(
                    "needs-link-newer",
                    create_time="2026-04-15T09:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "filtered-latest": {
                "summary": _archive_item(
                    "filtered-latest",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="filtered",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                exclude_filtered=True,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "needs-link-newer",
            "needs-link-older",
            "linked-newest",
        ]

    asyncio.run(scenario())


def test_list_archive_recordings_filters_by_hospital_code() -> None:
    async def scenario() -> None:
        archive_index = {
            "milan": {
                "summary": _archive_item(
                    "milan",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                    staff_hospital_code="6101",
                )
            },
            "yamei": {
                "summary": _archive_item(
                    "yamei",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                    staff_hospital_code="CSYM",
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code="CSYM",
                status=None,
                keyword=None,
                link_state=None,
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == ["yamei"]

    asyncio.run(scenario())


def test_list_archive_recordings_sorts_all_view_by_recording_time_desc() -> None:
    async def scenario() -> None:
        archive_index = {
            "linked-newest": {
                "summary": _archive_item(
                    "linked-newest",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "needs-link-older": {
                "summary": _archive_item(
                    "needs-link-older",
                    create_time="2026-04-14T10:00:00+08:00",
                    pipeline_status="transcribed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "needs-link-newer": {
                "summary": _archive_item(
                    "needs-link-newer",
                    create_time="2026-04-15T09:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "failed-latest": {
                "summary": _archive_item(
                    "failed-latest",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="failed",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "failed-latest",
            "linked-newest",
            "needs-link-newer",
            "needs-link-older",
        ]

    asyncio.run(scenario())


def test_list_archive_recordings_can_exclude_only_quality_filtered_items() -> None:
    async def scenario() -> None:
        archive_index = {
            "linked-newest": {
                "summary": _archive_item(
                    "linked-newest",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "filtered-latest": {
                "summary": _archive_item(
                    "filtered-latest",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="filtered",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
            "failed-middle": {
                "summary": _archive_item(
                    "failed-middle",
                    create_time="2026-04-15T10:30:00+08:00",
                    pipeline_status="failed",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                sort_mode=None,
                exclude_filtered=False,
                exclude_quality_filtered=True,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "linked-newest",
            "failed-middle",
        ]

    asyncio.run(scenario())


def test_wecom_archive_recordings_follow_staff_management_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        archive_index = {
            "managed-subordinate": {
                "summary": _archive_item(
                    "managed-subordinate",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="staff-managed",
                    staff_hospital_code="6501",
                )
            },
            "same-hospital-outside-scope": {
                "summary": _archive_item(
                    "same-hospital-outside-scope",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="staff-outside",
                    staff_hospital_code="6501",
                )
            },
            "manager-self": {
                "summary": _archive_item(
                    "manager-self",
                    create_time="2026-04-15T10:45:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="admin-6501",
                )
            },
            "unknown-staff": {
                "summary": _archive_item(
                    "unknown-staff",
                    create_time="2026-04-15T10:30:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="unknown-staff",
                )
            },
        }
        try:
            async with session_factory() as db:
                current_user = User(
                    username="hospital-admin",
                    hashed_password="x",
                    role="hospital_admin",
                    staff_id="admin-6501",
                    hospital_code="6501",
                )
                manager = Staff(id="admin-6501", name="Manager", hospital_code="6501", permission_role="hospital_admin")
                managed = Staff(id="staff-managed", name="Managed", hospital_code="6501", permission_role="staff")
                outside = Staff(id="staff-outside", name="Outside", hospital_code="6501", permission_role="staff")
                db.add_all([
                    current_user,
                    manager,
                    managed,
                    outside,
                    StaffManagementRelation(
                        hospital_code="6501",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=managed.id,
                    ),
                ])
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                        return_value=archive_index,
                    ),
                    patch(
                        "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                        AsyncMock(side_effect=lambda _db, items: items),
                    ),
                ):
                    page = await list_archive_recordings(
                        visit_id=None,
                        staff_id=None,
                        hospital_code=None,
                        status=None,
                        keyword=None,
                        link_state=None,
                        sort_mode=None,
                        exclude_filtered=False,
                        exclude_quality_filtered=False,
                        problem_only=False,
                        date_from=None,
                        date_to=None,
                        db=db,
                        current_user=current_user,
                        page=1,
                        page_size=10,
                    )
                    own_page = await list_archive_recordings(
                        visit_id=None,
                        staff_id=manager.id,
                        hospital_code=None,
                        status=None,
                        keyword=None,
                        link_state=None,
                        sort_mode=None,
                        exclude_filtered=False,
                        exclude_quality_filtered=False,
                        problem_only=False,
                        date_from=None,
                        date_to=None,
                        db=db,
                        current_user=current_user,
                        page=1,
                        page_size=10,
                    )

            assert [item.id for item in page.items] == [
                "manager-self",
                "managed-subordinate",
            ]
            assert [item.id for item in own_page.items] == ["manager-self"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_wecom_archive_recordings_exclude_higher_permission_managed_staff() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        archive_index = {
            "system-self": {
                "summary": _archive_item(
                    "system-self",
                    create_time="2026-04-15T10:30:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="system-manager",
                )
            },
            "normal-managed": {
                "summary": _archive_item(
                    "normal-managed",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="normal-managed",
                )
            },
            "super-admin-managed": {
                "summary": _archive_item(
                    "super-admin-managed",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="super-admin-staff",
                )
            },
        }

        try:
            async with session_factory() as db:
                current_user = User(
                    username="system-admin",
                    hashed_password="x",
                    role="system_admin",
                    staff_id="system-manager",
                )
                manager = Staff(id="system-manager", name="System manager", hospital_code="6101", permission_role="system_admin")
                normal = Staff(id="normal-managed", name="Normal", hospital_code="6101", permission_role="staff")
                super_admin = Staff(id="super-admin-staff", name="Super admin", hospital_code="6101", permission_role="super_admin")
                db.add_all([
                    current_user,
                    manager,
                    normal,
                    super_admin,
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=normal.id,
                    ),
                    StaffManagementRelation(
                        hospital_code="6101",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=super_admin.id,
                    ),
                ])
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                        return_value=archive_index,
                    ),
                    patch(
                        "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                        AsyncMock(side_effect=lambda _db, items: items),
                    ),
                ):
                    page = await list_archive_recordings(
                        visit_id=None,
                        staff_id=None,
                        hospital_code=None,
                        status=None,
                        keyword=None,
                        link_state=None,
                        sort_mode=None,
                        exclude_filtered=False,
                        exclude_quality_filtered=False,
                        problem_only=False,
                        date_from=None,
                        date_to=None,
                        db=db,
                        current_user=current_user,
                        page=1,
                        page_size=10,
                    )

            assert [item.id for item in page.items] == [
                "system-self",
                "normal-managed",
            ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_dingtalk_archive_recordings_follow_staff_management_scope() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        archive_index = {
            "managed-staff": {
                "summary": _archive_item(
                    "managed-staff",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="staff-managed",
                    staff_hospital_code="6501",
                )
            },
            "same-hospital-outside-scope": {
                "summary": _archive_item(
                    "same-hospital-outside-scope",
                    create_time="2026-04-15T11:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="staff-outside",
                    staff_hospital_code="6501",
                )
            },
            "manager-self": {
                "summary": _archive_item(
                    "manager-self",
                    create_time="2026-04-15T10:30:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                    staff_id="admin-6501",
                    staff_hospital_code="6501",
                )
            },
        }

        try:
            async with session_factory() as db:
                current_user = User(
                    username="hospital-admin",
                    hashed_password="x",
                    role="hospital_admin",
                    staff_id="admin-6501",
                    hospital_code="6501",
                )
                manager = Staff(id="admin-6501", name="Manager", hospital_code="6501", permission_role="hospital_admin")
                managed = Staff(id="staff-managed", name="Managed", hospital_code="6501", permission_role="staff")
                outside = Staff(id="staff-outside", name="Outside", hospital_code="6501", permission_role="staff")
                db.add_all([
                    current_user,
                    manager,
                    managed,
                    outside,
                    StaffManagementRelation(
                        hospital_code="6501",
                        manager_staff_id=manager.id,
                        subordinate_staff_id=managed.id,
                    ),
                ])
                await db.commit()

                with (
                    patch(
                        "smart_badge_api.api.routes.dingtalk._load_archive_recording_index",
                        return_value=archive_index,
                    ),
                    patch(
                        "smart_badge_api.api.routes.dingtalk._attach_archive_recording_bindings",
                        AsyncMock(side_effect=lambda _db, items: items),
                    ),
                ):
                    page = await dingtalk_routes.list_archive_recordings(
                        keyword=None,
                        status=None,
                        staff_id=None,
                        link_state=None,
                        exclude_filtered=False,
                        problem_only=False,
                        page=1,
                        page_size=10,
                        db=db,
                        current_user=current_user,
                    )
                    own_page = await dingtalk_routes.list_archive_recordings(
                        keyword=None,
                        status=None,
                        staff_id=manager.id,
                        link_state=None,
                        exclude_filtered=False,
                        problem_only=False,
                        page=1,
                        page_size=10,
                        db=db,
                        current_user=current_user,
                    )
                    with pytest.raises(HTTPException) as exc_info:
                        await dingtalk_routes.get_archive_recording_detail(
                            "same-hospital-outside-scope",
                            db=db,
                            current_user=current_user,
                        )

            assert [item.id for item in page.items] == ["manager-self", "managed-staff"]
            assert [item.id for item in own_page.items] == ["manager-self"]
            assert exc_info.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_build_archive_recording_summary_prefers_artifact_backed_status(tmp_path) -> None:
    transcript_path = tmp_path / "demo.transcript.json"
    transcript_path.write_text("{}", encoding="utf-8")
    analysis_path = tmp_path / "demo.result.json"
    analysis_path.write_text("{}", encoding="utf-8")

    summary = _build_archive_recording_summary(
        {
            "sn": "SN500",
            "fileId": "file-500",
            "audioPath": str(tmp_path / "demo.mp3"),
            "createTimeMs": 1775787010000,
        },
        {
            "deviceCode": "SN500",
            "status": "failed",
            "transcriptPath": str(transcript_path),
            "analysisResultPath": str(analysis_path),
            "errorMessage": "old error",
        },
    )

    assert summary is not None
    assert summary["pipeline_status"] == "analyzed"
    assert summary["error_message"] is None
    assert summary["has_transcript"] is True
    assert summary["has_analysis"] is True


def test_build_staged_archive_recording_summary_prefers_transcript_status(tmp_path) -> None:
    transcript_path = tmp_path / "demo.transcript.json"
    transcript_path.write_text("{}", encoding="utf-8")

    summary = _build_staged_archive_recording_summary(
        {
            "deviceCode": "SN501",
            "fileId": "file-501",
            "stagedFileName": "demo.mp3",
            "status": "failed",
            "transcriptPath": str(transcript_path),
            "errorMessage": "old error",
        }
    )

    assert summary is not None
    assert summary["pipeline_status"] == "transcribed"
    assert summary["error_message"] is None
    assert summary["has_transcript"] is True


def test_build_staged_archive_recording_summary_preserves_split_display_name() -> None:
    summary = _build_staged_archive_recording_summary(
        {
            "deviceCode": "SN502",
            "fileId": "split-child-1",
            "remoteFileName": "0506_155926_155926.mp3",
            "stagedFileName": "0506_155926_155926.mp3",
            "remoteCreatedAt": "2026-05-06T07:59:26+00:00",
            "status": "analyzed",
        }
    )

    assert summary is not None
    assert summary["display_file_name"] == "0506_155926_155926.mp3"


def test_build_staged_archive_recording_summary_infers_existing_result_file(tmp_path) -> None:
    transcript_path = tmp_path / "transcripts" / "SN777__file-777.transcript.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("{}", encoding="utf-8")
    result_path = tmp_path / "results" / "SN777__file-777.result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("{}", encoding="utf-8")

    with patch(
        "smart_badge_api.api.routes.dingtalk.get_settings",
        return_value=SimpleNamespace(
            dingtalk_audio_stage_path=tmp_path,
            upload_path=tmp_path,
            results_path=tmp_path / "results",
            resolve_path=lambda path: tmp_path / str(path),
        ),
    ):
        summary = _build_staged_archive_recording_summary(
            {
                "deviceCode": "SN777",
                "fileId": "file-777",
                "stagedFileName": "demo.mp3",
                "status": "failed",
                "transcriptPath": str(transcript_path),
                "analysisResultPath": None,
                "errorMessage": "old error",
            }
        )

    assert summary is not None
    assert summary["pipeline_status"] == "analyzed"
    assert summary["error_message"] is None
    assert summary["has_analysis"] is True


def test_build_archive_analysis_summary_uses_consultation_process_evaluation_scores() -> None:
    summary = {
        "create_time": "2026-04-19T08:00:00+08:00",
        "duration_ms": 90000,
    }
    transcript = {
        "durationMs": 90000,
        "utterances": [{"text": "你好"}, {"text": "你好"}],
    }
    analysis_result = {
        "consultation_process_evaluation": {
            "total_score": 7.5,
            "max_total_score": 9.0,
            "overall_score": 8.3,
            "overall_summary": "九点评价摘要",
        },
        "consultation_evaluation": {
            "total_score": 6.0,
            "max_total_score": 6.0,
            "overall_score": 7.0,
            "overall_summary": "旧版六维摘要",
        },
        "customer_demands": {
            "expectation": {
                "dialogue_type": "双人沟通",
            }
        },
        "customer_concerns": {"items": [{"content": "恢复期"}]},
        "customer_profile": {"tags": [{"category": "出生日期", "value": "1990-01-01"}]},
        "staff_recommendations": {"items": [{"recommendation": "热玛吉"}]},
    }

    result = _build_archive_analysis_summary(summary, transcript, analysis_result)

    assert result == {
        "recorded_at": "2026-04-19T08:00:00+08:00",
        "duration_ms": 90000,
        "duration_display": "1:30",
        "segment_count": 2,
        "overall_score": 8.3,
        "total_score": 7.5,
        "max_total_score": 9.0,
        "overall_summary": "九点评价摘要",
        "dialogue_type": "双人沟通",
        "focus_areas": [],
        "concern_count": 1,
        "tag_count": 1,
        "recommendation_count": 1,
    }


def test_resolve_archive_analysis_result_prefers_latest_analysis_task(tmp_path) -> None:
    async def scenario() -> None:
        stage_result_path = tmp_path / "stage.result.json"
        stage_result_path.write_text(
            json.dumps(
                {
                    "consultation_result": {
                        "chief_complaint_and_indications": {
                            "summary": "旧的适应症摘要",
                            "primary_demands": [],
                            "standardized_indications": [],
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        latest_result = {
            "consultation_result": {
                "chief_complaint_and_indications": {
                    "summary": "新的主诉与适应症摘要",
                    "primary_demands": ["想做水光针"],
                    "standardized_indications": ["皮肤（Y3）｜松弛下垂（SYZ3001）｜面部（BW3001）"],
                }
            }
        }

        class _FakeScalarResult:
            def __init__(self, value):
                self._value = value

            def first(self):
                return self._value

        class _FakeExecResult:
            def __init__(self, value):
                self._value = value

            def scalars(self):
                return _FakeScalarResult(self._value)

        class _FakeDb:
            async def execute(self, _stmt):
                return _FakeExecResult(
                    SimpleNamespace(
                        result=latest_result,
                        status="done",
                        completed_at=None,
                        updated_at=None,
                        created_at=None,
                    )
                )

            async def commit(self):
                return None

        fake_settings = SimpleNamespace(
            results_path=tmp_path / "results",
            upload_path=tmp_path / "uploads",
            resolve_path=lambda path: tmp_path / path,
        )

        with (
            patch(
                "smart_badge_api.api.routes.dingtalk.get_settings",
                return_value=fake_settings,
            ),
            patch(
                "smart_badge_api.api.routes.dingtalk._resolve_archive_manifest_file_path",
                return_value=stage_result_path,
            ),
        ):
            resolved = await _resolve_archive_analysis_result(
                _FakeDb(),  # type: ignore[arg-type]
                summary={"recording_id": "recording123"},
                manifest={"analysisResultPath": str(stage_result_path)},
            )

        assert resolved is not None
        chief = (
            resolved.get("consultation_result", {})
            .get("chief_complaint_and_indications", {})
        )
        assert chief.get("summary") == "新的主诉与适应症摘要"
        assert chief.get("primary_demands") == ["想做水光针"]
        assert json.loads(stage_result_path.read_text(encoding="utf-8")) == resolved
        persisted_path = fake_settings.results_path / "recording_recording123.result.json"
        assert json.loads(persisted_path.read_text(encoding="utf-8")) == resolved

    asyncio.run(scenario())


def test_list_archive_recordings_with_explicit_status_sorts_by_time_desc() -> None:
    async def scenario() -> None:
        archive_index = {
            "linked-newest": {
                "summary": _archive_item(
                    "linked-newest",
                    create_time="2026-04-15T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "needs-link-older": {
                "summary": _archive_item(
                    "needs-link-older",
                    create_time="2026-04-14T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "needs-link-newer": {
                "summary": _archive_item(
                    "needs-link-newer",
                    create_time="2026-04-15T09:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status="analyzed",
                keyword=None,
                link_state=None,
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "linked-newest",
            "needs-link-newer",
            "needs-link-older",
        ]

    asyncio.run(scenario())


def test_list_archive_recordings_date_grouped_link_state_sort_mode() -> None:
    async def scenario() -> None:
        archive_index = {
            "today-linked-newer": {
                "summary": _archive_item(
                    "today-linked-newer",
                    create_time="2026-04-17T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "today-filtered-latest": {
                "summary": _archive_item(
                    "today-filtered-latest",
                    create_time="2026-04-17T11:30:00+08:00",
                    pipeline_status="filtered",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
            "today-needs-link-older": {
                "summary": _archive_item(
                    "today-needs-link-older",
                    create_time="2026-04-17T09:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "yesterday-linked": {
                "summary": _archive_item(
                    "yesterday-linked",
                    create_time="2026-04-16T12:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "yesterday-needs-link": {
                "summary": _archive_item(
                    "yesterday-needs-link",
                    create_time="2026-04-16T08:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                sort_mode="date_grouped_link_state",
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "today-needs-link-older",
            "today-linked-newer",
            "today-filtered-latest",
            "yesterday-needs-link",
            "yesterday-linked",
        ]

    asyncio.run(scenario())


def test_list_archive_recordings_date_summaries_cover_filtered_result_not_current_page() -> None:
    async def scenario() -> None:
        archive_index = {
            "today-linked-newer": {
                "summary": _archive_item(
                    "today-linked-newer",
                    create_time="2026-04-17T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "today-linked-older": {
                "summary": _archive_item(
                    "today-linked-older",
                    create_time="2026-04-17T08:30:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "today-needs-link": {
                "summary": _archive_item(
                    "today-needs-link",
                    create_time="2026-04-17T09:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
            "yesterday-needs-link": {
                "summary": _archive_item(
                    "yesterday-needs-link",
                    create_time="2026-04-16T12:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=True,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                sort_mode="date_grouped_link_state",
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=2,
            )

        assert len(page.items) == 2
        summary_by_date = {item.date: item for item in page.date_summaries}
        assert summary_by_date["2026-04-17"].total == 3
        assert summary_by_date["2026-04-17"].needs_link_count == 1
        assert summary_by_date["2026-04-17"].linked_count == 2
        assert summary_by_date["2026-04-16"].total == 1

    asyncio.run(scenario())


def test_list_archive_recordings_fast_page_binds_only_first_candidate_batch() -> None:
    async def scenario() -> None:
        base_time = datetime(2026, 4, 17, 12, 0, 0)
        archive_index = {}
        for index in range(120):
            item_id = f"item-{index:03d}"
            archive_index[item_id] = {
                "summary": _archive_item(
                    item_id,
                    create_time=(base_time - timedelta(minutes=index)).isoformat() + "+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            }
        current_user = User(username="admin", hashed_password="x", role="super_admin")
        attach_mock = AsyncMock(side_effect=lambda _db, items: items)

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                attach_mock,
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status="analyzed",
                keyword=None,
                link_state=None,
                sort_mode=None,
                exclude_filtered=False,
                exclude_quality_filtered=True,
                problem_only=False,
                include_date_summaries=False,
                include_analysis_summary=True,
                fast_page=True,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=12,
            )

        assert [item.id for item in page.items] == [f"item-{index:03d}" for index in range(12)]
        assert page.total == 13
        assert page.pages == 2
        assert attach_mock.await_count == 1
        assert len(attach_mock.await_args.args[1]) == 50

    asyncio.run(scenario())


def test_list_archive_recordings_date_grouped_link_state_prioritizes_all_unlinked_items() -> None:
    async def scenario() -> None:
        archive_index = {
            "today-linked": {
                "summary": _archive_item(
                    "today-linked",
                    create_time="2026-04-17T10:00:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=True,
                )
            },
            "today-unlinked-normal": {
                "summary": _archive_item(
                    "today-unlinked-normal",
                    create_time="2026-04-17T09:30:00+08:00",
                    pipeline_status="analyzed",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
            "today-failed": {
                "summary": _archive_item(
                    "today-failed",
                    create_time="2026-04-17T11:00:00+08:00",
                    pipeline_status="failed",
                    needs_visit_link=False,
                    has_visit_link=False,
                )
            },
        }
        current_user = User(username="admin", hashed_password="x", role="super_admin")

        with (
            patch(
                "smart_badge_api.api.routes.recordings._load_archive_recording_index",
                return_value=archive_index,
            ),
            patch(
                "smart_badge_api.api.routes.recordings._attach_archive_recording_bindings",
                AsyncMock(side_effect=lambda _db, items: items),
            ),
        ):
            page = await list_archive_recordings(
                visit_id=None,
                staff_id=None,
                hospital_code=None,
                status=None,
                keyword=None,
                link_state=None,
                sort_mode="date_grouped_link_state",
                exclude_filtered=False,
                problem_only=False,
                date_from=None,
                date_to=None,
                db=None,  # type: ignore[arg-type]
                current_user=current_user,
                page=1,
                page_size=10,
            )

        assert [item.id for item in page.items] == [
            "today-failed",
            "today-unlinked-normal",
            "today-linked",
        ]

    asyncio.run(scenario())


def test_load_archive_recording_index_includes_stage_only_manifest(tmp_path) -> None:
    stage_root = tmp_path / "stage"
    manifest_dir = stage_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    audio_path = stage_root / "audio" / "SSYX41022508" / "dingtalk_SSYX41022508_file-123.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"fake-audio")

    manifest = {
        "stageKey": "SSYX41022508__file-123",
        "fileId": "file-123",
        "deviceCode": "SSYX41022508",
        "remoteFileName": "file-123.mp3",
        "stagedFileName": "dingtalk_SSYX41022508_file-123.mp3",
        "audioPath": str(audio_path),
        "remoteCreatedAt": "2026-04-16T08:22:46+00:00",
        "createdAt": "2026-04-16T09:41:43+00:00",
        "updatedAt": "2026-04-17T02:32:40+00:00",
        "status": "failed",
        "errorMessage": "CUDA failed",
        "durationMs": 4582440,
        "durationSeconds": 4582,
        "fileSize": 13745061,
        "staffId": "staff-1",
        "staffName": "钟露",
        "staffRole": "consultant",
    }
    (manifest_dir / "SSYX41022508__file-123.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    archive_root = tmp_path / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)

    with (
        patch("smart_badge_api.api.routes.dingtalk._dingtalk_stage_root", return_value=stage_root),
        patch("smart_badge_api.api.routes.dingtalk.get_archive_root", return_value=archive_root),
    ):
        index = _load_archive_recording_index()

    item_id = _archive_recording_id("SSYX41022508", "file-123")
    assert item_id in index

    payload = index[item_id]
    summary = payload["summary"]
    assert payload["archive_metadata"] is None
    assert payload["manifest"] == manifest
    assert summary["display_file_name"] == "0416_162246.mp3"
    assert summary["sn"] == "SSYX41022508"
    assert summary["device_code"] == "SSYX41022508"
    assert summary["pipeline_status"] == "failed"
    assert summary["error_message"] == "CUDA failed"
    assert summary["audio_path"] == str(audio_path)
    assert summary["stage_audio_path"] == str(audio_path)
    assert summary["create_time"] == "2026-04-16T08:22:46+00:00"
    assert summary["downloaded_at"] == "2026-04-16T09:41:43+00:00"


def test_load_archive_recording_index_treats_failed_manifest_with_completed_artifacts_as_analyzed(tmp_path) -> None:
    stage_root = tmp_path / "stage"
    manifest_dir = stage_root / "manifests"
    transcript_dir = stage_root / "transcripts"
    result_dir = stage_root / "results"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    stage_key = "SSYX41022508__file-456"
    audio_path = stage_root / "audio" / "SSYX41022508" / "dingtalk_SSYX41022508_file-456.mp3"
    transcript_path = transcript_dir / f"{stage_key}.transcript.json"
    result_path = result_dir / f"{stage_key}.result.json"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"fake-audio")
    transcript_path.write_text(json.dumps({"utterances": [{"text": "你好"}]}, ensure_ascii=False), encoding="utf-8")
    result_path.write_text(json.dumps({"customer_primary_demands": {"items": []}}, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "stageKey": stage_key,
        "fileId": "file-456",
        "deviceCode": "SSYX41022508",
        "remoteFileName": "file-456.mp3",
        "stagedFileName": "dingtalk_SSYX41022508_file-456.mp3",
        "audioPath": str(audio_path),
        "transcriptPath": str(transcript_path),
        "analysisResultPath": str(result_path),
        "remoteCreatedAt": "2026-04-16T08:22:46+00:00",
        "createdAt": "2026-04-16T09:41:43+00:00",
        "updatedAt": "2026-04-17T02:32:40+00:00",
        "status": "failed",
        "errorMessage": "old failure before retry",
    }
    (manifest_dir / f"{stage_key}.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    archive_root = tmp_path / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)

    with (
        patch("smart_badge_api.api.routes.dingtalk._dingtalk_stage_root", return_value=stage_root),
        patch("smart_badge_api.api.routes.dingtalk.get_archive_root", return_value=archive_root),
    ):
        index = _load_archive_recording_index()

    summary = index[_archive_recording_id("SSYX41022508", "file-456")]["summary"]
    assert summary["pipeline_status"] == "analyzed"
    assert summary["error_message"] is None
    assert summary["has_transcript"] is True
    assert summary["has_analysis"] is True
