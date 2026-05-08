import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from smart_badge_api.api.routes import analysis as analysis_routes
from smart_badge_api.api.routes.analysis import get_result, list_results
from smart_badge_api.api.routes.export import export_single_task
from smart_badge_api.api.routes.tasks import get_task, list_tasks, retry_task
from smart_badge_api.db.base import Base
from smart_badge_api.db.models import AnalysisTask, Customer, Recording, Staff, User, Visit
from smart_badge_api.visit_linking import sync_recording_visit_links


def test_staff_analysis_artifact_access_is_fully_scoped() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        try:
            async with session_factory() as db:
                staff_a = Staff(id="staff_a", name="顾问A", external_account="ADV001", permission_role="staff")
                staff_b = Staff(id="staff_b", name="顾问B", external_account="ADV002", permission_role="staff")
                customer_a = Customer(id="cust_a", name="客户A")
                customer_b = Customer(id="cust_b", name="客户B")
                visit_a = Visit(id="visit_a", customer_id=customer_a.id, consultant_id=staff_a.id, status="consulting")
                visit_b = Visit(id="visit_b", customer_id=customer_b.id, consultant_id=staff_b.id, status="consulting")
                recording_a = Recording(
                    id="recstaffa001",
                    visit_id=visit_a.id,
                    staff_id=staff_a.id,
                    file_name="a.mp3",
                    file_path="recordings/a.mp3",
                    status="analyzed",
                )
                recording_b = Recording(
                    id="recstaffb002",
                    visit_id=visit_b.id,
                    staff_id=staff_b.id,
                    file_name="b.mp3",
                    file_path="recordings/b.mp3",
                    status="analyzed",
                )
                current_user = User(
                    username="ADV001",
                    hashed_password="hashed",
                    display_name="顾问A",
                    staff_id=staff_a.id,
                    role="staff",
                    is_active=True,
                )
                now = datetime.now(timezone.utc)

                db.add_all([
                    staff_a,
                    staff_b,
                    customer_a,
                    customer_b,
                    visit_a,
                    visit_b,
                    recording_a,
                    recording_b,
                    current_user,
                    AnalysisTask(
                        id="task_a",
                        file_name="recording_recstaffa001.json",
                        file_path="uploads/analysis_input/recording_recstaffa001.json",
                        status="done",
                        overall_score=8.2,
                        completed_at=now,
                        result={"consultation_evaluation": {"overall_score": 8.2, "dimensions": []}},
                    ),
                    AnalysisTask(
                        id="task_b",
                        file_name="recording_recstaffb002.json",
                        file_path="uploads/analysis_input/recording_recstaffb002.json",
                        status="done",
                        overall_score=6.1,
                        completed_at=now,
                        result={"consultation_evaluation": {"overall_score": 6.1, "dimensions": []}},
                    ),
                    AnalysisTask(
                        id="task_ext",
                        file_name="external_payload.json",
                        file_path="uploads/external_payload.json",
                        status="done",
                        overall_score=5.4,
                        completed_at=now,
                        result={"consultation_evaluation": {"overall_score": 5.4, "dimensions": []}},
                    ),
                ])
                await db.flush()
                await sync_recording_visit_links(db, recording_a, [visit_a.id], primary_visit_id=visit_a.id, source="test")
                await sync_recording_visit_links(db, recording_b, [visit_b.id], primary_visit_id=visit_b.id, source="test")
                await db.commit()

                tasks = await list_tasks(db=db, page=1, page_size=20, current_user=current_user)
                assert tasks.total == 1
                assert [item.id for item in tasks.items] == ["task_a"]

                detail = await get_task("task_a", db=db, current_user=current_user)
                assert detail.id == "task_a"

                visible_export = await export_single_task("task_a", db=db, current_user=current_user)
                assert visible_export.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                assert visible_export.body

                for blocked_task_id in ("task_b", "task_ext"):
                    for fn in (get_task, export_single_task):
                        try:
                            await fn(blocked_task_id, db=db, current_user=current_user)
                        except HTTPException as exc:
                            assert exc.status_code == 404
                        else:
                            raise AssertionError("Blocked analysis artifact should not be visible to staff users")

                try:
                    await retry_task("task_a", db=db, current_user=current_user)
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    raise AssertionError("Staff users should not be able to rerun analysis tasks")

                result_payload = {
                    "customer_primary_demands": {"summary": "主诉总结", "items": []},
                    "staff_recommendations": {"summary": "推荐总结", "items": []},
                    "standardized_indications": {"summary": "适应症总结", "items": []},
                    "customer_demands": {"focus_areas": [], "expectation": {"dialogue_type": "初诊咨询"}},
                    "customer_concerns": {"summary": "顾虑总结", "items": []},
                    "customer_profile": {"tags": []},
                    "consultation_evaluation": {"overall_score": 8.5, "overall_summary": "评价总结", "dimensions": []},
                }

                with TemporaryDirectory() as tmp_dir:
                    base_path = Path(tmp_dir)
                    results_dir = base_path / "results"
                    upload_dir = base_path / "uploads"
                    results_dir.mkdir(parents=True, exist_ok=True)
                    upload_dir.mkdir(parents=True, exist_ok=True)

                    for file_id in ("recording_recstaffa001", "recording_recstaffb002", "external_payload"):
                        (results_dir / f"{file_id}.result.json").write_text(
                            json.dumps(result_payload, ensure_ascii=False),
                            encoding="utf-8",
                        )

                    original_results_dir = analysis_routes._results_dir
                    original_raw_dir = analysis_routes._raw_dir
                    analysis_routes._results_dir = lambda: results_dir
                    analysis_routes._raw_dir = lambda: upload_dir
                    try:
                        listing = await list_results(
                            sort_by="time",
                            sort_order="desc",
                            min_score=None,
                            max_score=None,
                            page=1,
                            page_size=20,
                            db=db,
                            current_user=current_user,
                        )
                        assert listing["total"] == 1
                        assert [item["file_id"] for item in listing["items"]] == ["recording_recstaffa001"]

                        own = await get_result("recording_recstaffa001", db=db, current_user=current_user)
                        assert own["file_id"] == "recording_recstaffa001"

                        for blocked_file_id in ("recording_recstaffb002", "external_payload"):
                            try:
                                await get_result(blocked_file_id, db=db, current_user=current_user)
                            except HTTPException as exc:
                                assert exc.status_code == 404
                            else:
                                raise AssertionError("Blocked analysis result should not be visible to staff users")
                    finally:
                        analysis_routes._results_dir = original_results_dir
                        analysis_routes._raw_dir = original_raw_dir
        finally:
            await engine.dispose()

    asyncio.run(scenario())
