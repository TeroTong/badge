"""Production analysis entrypoint.

The formal recording analysis chain uses the Agent pipeline by default.
Set ``ANALYSIS_PIPELINE=staged`` to roll back to the staged evidence/judgment
pipeline without changing worker code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smart_badge_api.analysis.agent_pipeline import analyze_transcript_agent
from smart_badge_api.analysis.staged_pipeline import analyze_transcript_staged
from smart_badge_api.core.config import get_settings


def _run_selected_pipeline(
    path: str | Path,
    *,
    system_prompt: str | None,
    staff_context: dict[str, Any] | None,
) -> dict[str, Any]:
    pipeline = get_settings().analysis_pipeline
    if pipeline == "agent":
        return analyze_transcript_agent(
            path,
            system_prompt=system_prompt,
            staff_context=staff_context,
        )
    return analyze_transcript_staged(
        path,
        system_prompt=system_prompt,
        staff_context=staff_context,
    )


def analyze_transcript_for_production(
    path: str | Path,
    *,
    system_prompt: str | None = None,
    staff_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze one transcript using the current production chain.

    Returns the same analysis_result payload shape used by the rest of the app.
    The staged artifact is intentionally not returned here because workers and
    API pages expect the normalized analysis result, not debug envelopes.
    """
    analyzed = _run_selected_pipeline(
        path,
        system_prompt=system_prompt,
        staff_context=staff_context,
    )
    result = analyzed.get("analysis_result") if isinstance(analyzed, dict) else None
    if not isinstance(result, dict) or not result:
        raise RuntimeError("生产分析链路未返回有效的 analysis_result")

    debug = result.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        debug["production_chain"] = analyzed.get("pipeline", get_settings().analysis_pipeline)
        debug.setdefault("configured_pipeline", get_settings().analysis_pipeline)
        debug.setdefault("llm_call_plan", analyzed.get("llm_call_plan", {}))
        debug.setdefault("input_stats", analyzed.get("input_stats", {}))
    return result
