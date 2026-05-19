from __future__ import annotations

from types import SimpleNamespace

from smart_badge_api.analysis import production


def _pipeline_payload(name: str) -> dict:
    return {
        "pipeline": name,
        "analysis_result": {
            "staged_pipeline_debug": {},
        },
        "llm_call_plan": {"pipeline": name},
        "input_stats": {"source": "unit-test"},
    }


def test_production_uses_agent_pipeline_by_default(monkeypatch, tmp_path):
    calls: list[str] = []

    monkeypatch.setattr(production, "get_settings", lambda: SimpleNamespace(analysis_pipeline="agent"))
    monkeypatch.setattr(
        production,
        "analyze_transcript_agent",
        lambda *args, **kwargs: calls.append("agent") or _pipeline_payload("agent_pipeline_v2_gpt52"),
    )
    monkeypatch.setattr(
        production,
        "analyze_transcript_staged",
        lambda *args, **kwargs: calls.append("staged") or _pipeline_payload("staged_evidence_judgment_v1_gpt52"),
    )

    result = production.analyze_transcript_for_production(tmp_path / "input.json")

    assert calls == ["agent"]
    debug = result["staged_pipeline_debug"]
    assert debug["production_chain"] == "agent_pipeline_v2_gpt52"
    assert debug["configured_pipeline"] == "agent"


def test_production_can_roll_back_to_staged_pipeline(monkeypatch, tmp_path):
    calls: list[str] = []

    monkeypatch.setattr(production, "get_settings", lambda: SimpleNamespace(analysis_pipeline="staged"))
    monkeypatch.setattr(
        production,
        "analyze_transcript_agent",
        lambda *args, **kwargs: calls.append("agent") or _pipeline_payload("agent_pipeline_v2_gpt52"),
    )
    monkeypatch.setattr(
        production,
        "analyze_transcript_staged",
        lambda *args, **kwargs: calls.append("staged") or _pipeline_payload("staged_evidence_judgment_v1_gpt52"),
    )

    result = production.analyze_transcript_for_production(tmp_path / "input.json")

    assert calls == ["staged"]
    debug = result["staged_pipeline_debug"]
    assert debug["production_chain"] == "staged_evidence_judgment_v1_gpt52"
    assert debug["configured_pipeline"] == "staged"
