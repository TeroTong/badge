"""Production analysis entrypoint.

The formal recording analysis chain now uses the staged evidence/judgment
pipeline. The legacy one-pass pipeline is kept in ``pipeline.py`` for tests,
manual comparison, and rollback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smart_badge_api.analysis.staged_pipeline import analyze_transcript_staged


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
    staged = analyze_transcript_staged(
        path,
        system_prompt=system_prompt,
        staff_context=staff_context,
    )
    result = staged.get("analysis_result") if isinstance(staged, dict) else None
    if not isinstance(result, dict) or not result:
        raise RuntimeError("备用分析链路未返回有效的 analysis_result")

    debug = result.setdefault("staged_pipeline_debug", {})
    if isinstance(debug, dict):
        debug.setdefault("production_chain", staged.get("pipeline", "staged"))
        debug.setdefault("llm_call_plan", staged.get("llm_call_plan", {}))
        debug.setdefault("input_stats", staged.get("input_stats", {}))
    return result
