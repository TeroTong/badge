from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from smart_badge_api.analysis.consultation_evaluation import rebuild_consultation_evaluation
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.analysis.customer_profile_score_sync import _result_path
from smart_badge_api.db.models import AnalysisTask
from smart_badge_api.db.session import _session_factory


async def main() -> None:
    async with _session_factory() as db:
        tasks = (
            await db.execute(
                select(AnalysisTask).where(
                    AnalysisTask.status == "done",
                    AnalysisTask.result.is_not(None),
                )
            )
        ).scalars().all()

        updated = 0
        for task in tasks:
            original_result = task.result or {}
            normalized_result = normalize_analysis_result(original_result) or original_result
            normalized_result["consultation_evaluation"] = rebuild_consultation_evaluation(
                normalized_result,
                historical_profile_tags=[],
            )
            overall_score = normalized_result["consultation_evaluation"].get("overall_score")
            normalized_overall_score = float(overall_score) if isinstance(overall_score, (int, float)) else None

            if normalized_result == original_result and task.overall_score == normalized_overall_score:
                continue

            task.result = normalized_result
            task.overall_score = normalized_overall_score
            if task.file_name.startswith("recording_") and task.file_name.endswith(".json"):
                result_file = _result_path(task.file_name.removeprefix("recording_").removesuffix(".json"))
                if result_file.exists():
                    result_file.write_text(
                        json.dumps(normalized_result, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            updated += 1

        await db.commit()
        print(f"updated_tasks={updated}")


if __name__ == "__main__":
    asyncio.run(main())
