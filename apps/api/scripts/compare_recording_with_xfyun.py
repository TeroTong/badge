from __future__ import annotations

import argparse
import asyncio
import difflib
import html
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.analysis.pipeline import analyze_transcript
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.asr.domain_terms import apply_medical_aesthetic_term_normalization
from smart_badge_api.asr.speaker_role_resolver import resolve_speaker_roles
from smart_badge_api.asr.speaker_voiceprint import apply_staff_voiceprints
from smart_badge_api.asr.xfyun_asr_provider import transcribe_audio as transcribe_audio_with_xfyun
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import AnalysisTask, Recording, Transcript
from smart_badge_api.db.session import _session_factory
from smart_badge_api.recording_analysis_service import build_analysis_payload_from_utterances


@dataclass(slots=True)
class ComparisonPaths:
    root: str
    metadata: str
    current_transcript_json: str
    current_transcript_text: str
    current_analysis_json: str
    xfyun_transcript_json: str
    xfyun_transcript_text: str
    xfyun_analysis_input_json: str
    xfyun_analysis_json: str
    transcript_diff: str
    analysis_diff: str
    report_markdown: str
    report_html: str


def _format_ms(value: int | None) -> str:
    total_ms = max(int(value or 0), 0)
    minutes, ms_remainder = divmod(total_ms, 60_000)
    seconds, milliseconds = divmod(ms_remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _format_duration(value: int | None) -> str:
    total_seconds = max(int((value or 0) / 1000), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return "-"
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _serialize_json(value: Any, path: Path) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _speaker_counter(utterances: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(item.get("speaker") or item.get("speaker_id") or "unknown") for item in utterances)
    return dict(counter)


def _speaker_counter_text(utterances: list[dict[str, Any]]) -> str:
    counter = _speaker_counter(utterances)
    if not counter:
        return "未识别"
    return " / ".join(f"{speaker}: {count}" for speaker, count in counter.items())


def _utterance_lines(utterances: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in utterances:
        begin_ms = int(item.get("begin_ms") or 0)
        end_ms = int(item.get("end_ms") or begin_ms)
        speaker = str(item.get("speaker") or item.get("speaker_id") or "unknown")
        text = str(item.get("text") or "").strip()
        lines.append(f"{_format_ms(begin_ms)}-{_format_ms(end_ms)} [{speaker}] {text}")
    return lines


def _write_text(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_diff(path: Path, current_lines: list[str], variant_lines: list[str], *, fromfile: str, tofile: str) -> None:
    diff = difflib.unified_diff(current_lines, variant_lines, fromfile=fromfile, tofile=tofile, lineterm="")
    path.write_text("\n".join(diff) + "\n", encoding="utf-8")


def _extract_labels(items: list[dict[str, Any]], *keys: str) -> list[str]:
    labels: list[str] = []
    for item in items:
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                labels.append(value)
                break
    return labels


def _analysis_snapshot(result: dict[str, Any] | None) -> dict[str, Any]:
    payload = result or {}
    profile_tags = payload.get("customer_profile", {}).get("tags") or []
    return {
        "customer_primary_demands": _extract_labels(payload.get("customer_primary_demands", {}).get("items") or [], "demand"),
        "standardized_indications": _extract_labels(
            payload.get("standardized_indications", {}).get("items") or [],
            "indication_name",
        ),
        "customer_concerns": _extract_labels(payload.get("customer_concerns", {}).get("items") or [], "content"),
        "customer_profile_tags": [f'{item.get("category")}={item.get("value")}' for item in profile_tags if item.get("category") and item.get("value")],
        "staff_recommendations": _extract_labels(
            payload.get("staff_recommendations", {}).get("items") or [],
            "recommendation",
            "product_or_solution",
        ),
        "consultation_evaluation": {
            "total_score": payload.get("consultation_evaluation", {}).get("total_score"),
            "max_total_score": payload.get("consultation_evaluation", {}).get("max_total_score"),
            "overall_score": payload.get("consultation_evaluation", {}).get("overall_score"),
            "overall_summary": payload.get("consultation_evaluation", {}).get("overall_summary"),
        },
        "consultation_process_evaluation": {
            "total_score": payload.get("consultation_process_evaluation", {}).get("total_score"),
            "max_total_score": payload.get("consultation_process_evaluation", {}).get("max_total_score"),
            "overall_score": payload.get("consultation_process_evaluation", {}).get("overall_score"),
            "overall_summary": payload.get("consultation_process_evaluation", {}).get("overall_summary"),
        },
        "consultation_result": {
            "deal_status": payload.get("consultation_result", {}).get("deal_outcome", {}).get("status"),
            "deal_summary": payload.get("consultation_result", {}).get("deal_outcome", {}).get("summary"),
            "recommended_plans": _extract_labels(
                payload.get("consultation_result", {}).get("recommended_plan", {}).get("items") or [],
                "plan",
            ),
        },
    }


def _analysis_result_from_task(task: AnalysisTask | None) -> dict[str, Any]:
    if task and isinstance(task.result, dict):
        return task.result
    return {}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _normalize_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _list_diff(left: Sequence[str], right: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    left_list = _dedupe_strings(left)
    right_list = _dedupe_strings(right)
    left_set = set(left_list)
    right_set = set(right_list)
    shared = [item for item in left_list if item in right_set]
    left_only = [item for item in left_list if item not in right_set]
    right_only = [item for item in right_list if item not in left_set]
    return shared, left_only, right_only


def _relative_href(base_dir: Path, target: Path) -> str:
    return os.path.relpath(target, base_dir).replace(os.sep, "/")


def _render_chip(text: str, *, tone: str = "neutral") -> str:
    return f'<span class="cmp-chip cmp-chip--{tone}">{html.escape(text)}</span>'


def _render_empty(text: str) -> str:
    return f'<div class="cmp-empty">{html.escape(text)}</div>'


def _render_metric(label: str, value: str, subtext: str | None = None) -> str:
    subtext_html = f'<small>{html.escape(subtext)}</small>' if subtext else ""
    return (
        '<div class="cmp-metric">'
        f"<label>{html.escape(label)}</label>"
        f"<strong>{html.escape(value)}</strong>"
        f"{subtext_html}"
        "</div>"
    )


def _render_evidence(evidence: str | Sequence[str] | None, *, label: str = "查看依据") -> str:
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        lines = _dedupe_strings([evidence])
    else:
        lines = _dedupe_strings(evidence)
    if not lines:
        return ""
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return (
        '<details class="cmp-evidence">'
        f"<summary>{html.escape(label)}</summary>"
        f'<div class="cmp-evidence__body">{body}</div>'
        "</details>"
    )


def _render_text_block(text: str | None, *, class_name: str = "cmp-section-summary") -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    return f'<p class="{class_name}">{html.escape(normalized)}</p>'


def _render_badge_list(values: Sequence[str], *, tone: str = "neutral", empty_text: str = "暂无") -> str:
    items = _dedupe_strings(values)
    if not items:
        return _render_empty(empty_text)
    chips = "".join(_render_chip(item, tone=tone) for item in items)
    return f'<div class="cmp-chip-list">{chips}</div>'


def _render_diff_groups(shared: Sequence[str], left_only: Sequence[str], right_only: Sequence[str]) -> str:
    groups: list[str] = []
    if shared:
        groups.append(
            '<div class="cmp-diff-group">'
            '<span class="cmp-diff-group__label">共同识别</span>'
            f"{_render_badge_list(shared, tone='neutral')}"
            "</div>"
        )
    if left_only:
        groups.append(
            '<div class="cmp-diff-group">'
            '<span class="cmp-diff-group__label">仅当前结果</span>'
            f"{_render_badge_list(left_only, tone='current')}"
            "</div>"
        )
    if right_only:
        groups.append(
            '<div class="cmp-diff-group">'
            '<span class="cmp-diff-group__label">仅讯飞旁路</span>'
            f"{_render_badge_list(right_only, tone='variant')}"
            "</div>"
        )
    return "".join(groups) if groups else _render_empty("两边目前没有明显差异")


def _render_change_card(
    *,
    title: str,
    left_label: str,
    left_value: str,
    right_label: str,
    right_value: str,
    body_html: str = "",
) -> str:
    return (
        '<article class="cmp-change-card">'
        f'<div class="cmp-change-card__title">{html.escape(title)}</div>'
        '<div class="cmp-change-card__compare">'
        '<div class="cmp-change-card__cell cmp-change-card__cell--current">'
        f"<small>{html.escape(left_label)}</small>"
        f"<strong>{html.escape(left_value)}</strong>"
        "</div>"
        '<div class="cmp-change-card__cell cmp-change-card__cell--variant">'
        f"<small>{html.escape(right_label)}</small>"
        f"<strong>{html.escape(right_value)}</strong>"
        "</div>"
        "</div>"
        f"{body_html}"
        "</article>"
    )


def _render_artifact_link(label: str, href: str) -> str:
    file_name = href.split("/")[-1]
    return (
        '<a class="cmp-link-card" href="'
        f'{html.escape(href, quote=True)}">'
        f"<strong>{html.escape(label)}</strong>"
        f"<span>{html.escape(file_name)}</span>"
        "</a>"
    )


def _render_item_card(
    *,
    title: str,
    meta: Sequence[str] | None = None,
    summary: str | None = None,
    evidence: str | Sequence[str] | None = None,
) -> str:
    meta_html = ""
    meta_values = _dedupe_strings(meta or [])
    if meta_values:
        meta_html = f'<div class="cmp-item-card__meta">{" · ".join(html.escape(item) for item in meta_values)}</div>'
    summary_html = _render_text_block(summary, class_name="cmp-item-card__summary")
    return (
        '<div class="cmp-item-card">'
        f'<div class="cmp-item-card__title">{html.escape(title)}</div>'
        f"{meta_html}"
        f"{summary_html}"
        f"{_render_evidence(evidence)}"
        "</div>"
    )


def _render_demand_items(items: Sequence[dict[str, Any]]) -> str:
    if not items:
        return _render_empty("当前没有识别到明确主诉")
    cards = []
    for item in items:
        demand = _normalize_text(item.get("demand")) or "未识别诉求"
        meta: list[str] = []
        if item.get("body_part"):
            meta.append(f"部位：{item['body_part']}")
        if item.get("priority"):
            meta.append(f"优先级 {item['priority']}")
        cards.append(
            _render_item_card(
                title=demand,
                meta=meta,
                evidence=item.get("evidence"),
            )
        )
    return f'<div class="cmp-card-list">{ "".join(cards) }</div>'


def _render_indication_items(items: Sequence[dict[str, Any]]) -> str:
    if not items:
        return _render_empty("当前没有识别到标准化适应症")
    cards = []
    for item in items:
        indication_name = _normalize_text(item.get("indication_name")) or "未命名适应症"
        body_part = _normalize_text(item.get("body_part_name"))
        department = _normalize_text(item.get("department_name"))
        title = indication_name if not body_part else f"{indication_name} · {body_part}"
        meta = [part for part in [department, item.get("indication_code"), item.get("body_part_code")] if part]
        cards.append(
            _render_item_card(
                title=title,
                meta=meta,
                evidence=item.get("evidence"),
            )
        )
    return f'<div class="cmp-card-list">{ "".join(cards) }</div>'


def _render_concern_items(items: Sequence[dict[str, Any]]) -> str:
    if not items:
        return _render_empty("当前没有识别到客户顾虑")
    cards = []
    for item in items:
        content = _normalize_text(item.get("content")) or "未命名顾虑"
        concern_type = _normalize_text(item.get("type"))
        cards.append(
            _render_item_card(
                title=content,
                meta=[f"类型：{concern_type}"] if concern_type else None,
                evidence=item.get("evidence"),
            )
        )
    return f'<div class="cmp-card-list">{ "".join(cards) }</div>'


def _render_recommendation_items(items: Sequence[dict[str, Any]]) -> str:
    if not items:
        return _render_empty("当前没有识别到明确推荐方案")
    cards = []
    for item in items:
        recommendation = _normalize_text(item.get("plan") or item.get("recommendation") or item.get("product_or_solution")) or "未命名方案"
        meta: list[str] = []
        if item.get("product_or_solution") and item.get("product_or_solution") != recommendation:
            meta.append(str(item["product_or_solution"]))
        if item.get("body_part"):
            meta.append(f"部位：{item['body_part']}")
        if item.get("customer_response"):
            meta.append(f"客户反馈：{item['customer_response']}")
        if item.get("demand_priority"):
            priorities = ",".join(_format_number(value) for value in item["demand_priority"])
            meta.append(f"诉求优先级：{priorities}")
        cards.append(
            _render_item_card(
                title=recommendation,
                meta=meta,
                evidence=item.get("evidence"),
            )
        )
    return f'<div class="cmp-card-list">{ "".join(cards) }</div>'


def _render_profile_tags(profile: dict[str, Any], profile_summary: dict[str, Any]) -> str:
    tags = profile.get("tags") or []
    if not tags and not profile_summary:
        return _render_empty("当前没有提取到画像标签")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in tags:
        category = _normalize_text(item.get("category")) or "其他"
        grouped.setdefault(category, []).append(item)

    blocks: list[str] = []
    summary_bits: list[str] = []
    if profile_summary.get("age") or profile.get("age"):
        age = _normalize_text(profile_summary.get("age") or profile.get("age"))
        if age:
            summary_bits.append(f"年龄：{age}")
    if profile_summary.get("extracted_tag_count") is not None:
        summary_bits.append(f"标签数：{_format_number(profile_summary.get('extracted_tag_count'))}")
    if summary_bits:
        blocks.append(f'<div class="cmp-inline-note">{html.escape(" · ".join(summary_bits))}</div>')
    if profile_summary.get("summary"):
        blocks.append(_render_text_block(str(profile_summary["summary"]), class_name="cmp-inline-summary"))
    if profile_summary.get("age_evidence") or profile.get("age_evidence"):
        blocks.append(_render_evidence(profile_summary.get("age_evidence") or profile.get("age_evidence"), label="查看年龄依据"))

    for category, category_tags in grouped.items():
        tag_cards = []
        for item in category_tags:
            value = _normalize_text(item.get("value")) or "未命名标签"
            meta = [f"权重 {item['weight_level']}"] if item.get("weight_level") is not None else []
            tag_cards.append(
                _render_item_card(
                    title=value,
                    meta=meta,
                    evidence=item.get("evidence"),
                )
            )
        blocks.append(
            '<div class="cmp-profile-group">'
            f'<div class="cmp-profile-group__title">{html.escape(category)}</div>'
            f'<div class="cmp-card-list">{ "".join(tag_cards) }</div>'
            "</div>"
        )
    return "".join(blocks)


def _render_score_line(label: str, score: Any, max_score: Any) -> str:
    if score is None or max_score in (None, 0):
        percent = 0
    else:
        percent = max(0.0, min(float(score) / float(max_score) * 100.0, 100.0))
    return (
        '<div class="cmp-score-line">'
        '<div class="cmp-score-line__row">'
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(_format_number(score))} / {html.escape(_format_number(max_score))}</strong>"
        "</div>"
        '<div class="cmp-score-line__track">'
        f'<span style="width:{percent:.1f}%"></span>'
        "</div>"
        "</div>"
    )


def _render_consultation_dimensions(dimensions: Sequence[dict[str, Any]]) -> str:
    if not dimensions:
        return _render_empty("当前没有咨询维度评分")
    cards = []
    for item in dimensions:
        issue_text = "；".join(
            _dedupe_strings(issue.get("description") for issue in item.get("issues") or [] if issue.get("description"))
        )
        cards.append(
            '<div class="cmp-eval-card">'
            '<div class="cmp-eval-card__head">'
            f"<strong>{html.escape(_normalize_text(item.get('name')) or '未命名维度')}</strong>"
            f"<span>{html.escape(_normalize_text(item.get('status')) or '待补充')}</span>"
            "</div>"
            f"{_render_score_line('得分', item.get('point_score'), item.get('max_score'))}"
            f"{_render_text_block(_normalize_text(item.get('summary')) or issue_text, class_name='cmp-eval-card__summary')}"
            f"{_render_evidence([issue.get('evidence') for issue in item.get('issues') or [] if issue.get('evidence')], label='查看问题依据')}"
            "</div>"
        )
    return f'<div class="cmp-eval-grid">{ "".join(cards) }</div>'


def _render_process_sections(sections: Sequence[dict[str, Any]]) -> str:
    if not sections:
        return _render_empty("当前没有面诊过程评分")

    blocks: list[str] = []
    for section in sections:
        checkpoints = section.get("checkpoints") or []
        checkpoint_cards: list[str] = []
        for checkpoint in checkpoints:
            issue_texts = _dedupe_strings(issue.get("description") for issue in checkpoint.get("issues") or [] if issue.get("description"))
            checkpoint_cards.append(
                '<div class="cmp-process-card">'
                '<div class="cmp-process-card__head">'
                '<div>'
                f"<strong>{html.escape((_normalize_text(checkpoint.get('code')) + ' ' + _normalize_text(checkpoint.get('name'))).strip() or '未命名节点')}</strong>"
                f'<span>{html.escape(_normalize_text(checkpoint.get("status")) or "待补充")}</span>'
                "</div>"
                f"<em>{html.escape(_format_number(checkpoint.get('point_score')))} / {html.escape(_format_number(checkpoint.get('max_score')))}</em>"
                "</div>"
                f"{_render_text_block(_normalize_text(checkpoint.get('summary')) or '当前没有补充说明', class_name='cmp-process-card__summary')}"
                f"{_render_text_block('；'.join(issue_texts), class_name='cmp-process-card__issue') if issue_texts else ''}"
                f"{_render_evidence(checkpoint.get('evidence'), label='查看节点依据')}"
                f"{_render_evidence([issue.get('evidence') for issue in checkpoint.get('issues') or [] if issue.get('evidence')], label='查看问题依据')}"
                "</div>"
            )
        blocks.append(
            '<details class="cmp-process-section">'
            '<summary>'
            '<div>'
            f"<strong>{html.escape((_normalize_text(section.get('code')) + ' ' + _normalize_text(section.get('name'))).strip() or '未命名阶段')}</strong>"
            f'<span>{html.escape(_normalize_text(section.get("status")) or "待补充")}</span>'
            "</div>"
            f"<em>{html.escape(_format_number(section.get('point_score')))} / {html.escape(_format_number(section.get('max_score')))}</em>"
            "</summary>"
            '<div class="cmp-process-section__body">'
            f"{_render_text_block(section.get('summary'))}"
            f'<div class="cmp-process-section__list">{ "".join(checkpoint_cards) }</div>'
            "</div>"
            "</details>"
        )
    return "".join(blocks)


def _render_fold(title: str, body_html: str, *, subtitle: str | None = None, open: bool = False) -> str:
    open_attr = " open" if open else ""
    subtitle_html = f'<small>{html.escape(subtitle)}</small>' if subtitle else ""
    return (
        f'<details class="cmp-fold"{open_attr}>'
        "<summary>"
        f"<span>{html.escape(title)}</span>"
        f"{subtitle_html}"
        "</summary>"
        f'<div class="cmp-fold__body">{body_html}</div>'
        "</details>"
    )


def _render_provider_panel(
    *,
    label: str,
    provider_name: str,
    accent: str,
    utterances: list[dict[str, Any]],
    full_text: str,
    duration_ms: int | None,
    result: dict[str, Any],
) -> str:
    customer_primary_demands = result.get("customer_primary_demands") or {}
    standardized_indications = result.get("standardized_indications") or {}
    customer_concerns = result.get("customer_concerns") or {}
    customer_profile = result.get("customer_profile") or {}
    consultation_evaluation = result.get("consultation_evaluation") or {}
    consultation_result = result.get("consultation_result") or {}
    consultation_process_evaluation = result.get("consultation_process_evaluation") or {}
    staff_recommendations = result.get("staff_recommendations") or {}
    chief = consultation_result.get("chief_complaint_and_indications") or {}
    deal_factors = consultation_result.get("deal_factors") or {}
    recommended_plan = consultation_result.get("recommended_plan") or {}
    deal_outcome = consultation_result.get("deal_outcome") or {}
    customer_profile_summary = consultation_result.get("customer_profile_summary") or {}

    hero_summary = _normalize_text(
        deal_outcome.get("summary")
        or consultation_evaluation.get("overall_summary")
        or consultation_process_evaluation.get("overall_summary")
    ) or "当前没有整理出明确综合结论"
    recommendation_count_label = f"推荐 {len(recommended_plan.get('items') or [])} 项"
    consultation_overall_text = f"{_format_number(consultation_evaluation.get('overall_score'))} 分"
    consultation_total_text = (
        f"{_format_number(consultation_evaluation.get('total_score'))} / "
        f"{_format_number(consultation_evaluation.get('max_total_score'))}"
    )
    process_overall_text = f"{_format_number(consultation_process_evaluation.get('overall_score'))} 分"
    process_total_text = (
        f"{_format_number(consultation_process_evaluation.get('total_score'))} / "
        f"{_format_number(consultation_process_evaluation.get('max_total_score'))}"
    )

    overview_metrics = "".join(
        [
            _render_metric("Provider", provider_name),
            _render_metric("句子数", _format_number(len(utterances))),
            _render_metric("文本长度", _format_number(len(full_text))),
            _render_metric("时长", _format_duration(duration_ms)),
        ]
    )

    result_body = "".join(
        [
            _render_text_block(chief.get("summary") or customer_primary_demands.get("summary")),
            '<div class="cmp-subsection"><h4>主诉</h4>',
            _render_demand_items(customer_primary_demands.get("items") or []),
            "</div>",
            '<div class="cmp-subsection"><h4>标准化适应症</h4>',
            _render_indication_items(standardized_indications.get("items") or []),
            "</div>",
        ]
    )

    concern_body = "".join(
        [
            _render_text_block(deal_factors.get("summary") or customer_concerns.get("summary")),
            '<div class="cmp-subsection"><h4>客户顾虑</h4>',
            _render_concern_items(customer_concerns.get("items") or []),
            "</div>",
            '<div class="cmp-subsection"><h4>客户画像</h4>',
            _render_profile_tags(customer_profile, customer_profile_summary),
            "</div>",
        ]
    )

    recommendation_body = "".join(
        [
            '<div class="cmp-result-pill-row">'
            f"{_render_chip(_normalize_text(deal_outcome.get('status')) or '未明确', tone='accent')}"
            f"{_render_chip(recommendation_count_label, tone='neutral')}"
            "</div>",
            _render_text_block(recommended_plan.get("summary")),
            _render_text_block(deal_outcome.get("summary"), class_name="cmp-inline-summary"),
            '<div class="cmp-subsection"><h4>推荐方案</h4>',
            _render_recommendation_items(recommended_plan.get("items") or staff_recommendations.get("items") or []),
            "</div>",
            '<div class="cmp-subsection"><h4>结果要点</h4>',
            _render_badge_list(deal_outcome.get("deal_items") or [], tone="accent", empty_text="当前没有明确成交项目"),
            _render_badge_list(deal_factors.get("decision_factors") or [], tone="neutral", empty_text="当前没有单独整理出其他决策因素"),
            "</div>",
        ]
    )

    consultation_eval_body = "".join(
        [
            '<div class="cmp-score-hero">'
            f"{_render_metric('总分', consultation_overall_text, _normalize_text(consultation_evaluation.get('overall_summary')) or None)}"
            f"{_render_metric('明细', consultation_total_text)}"
            "</div>",
            _render_consultation_dimensions(consultation_evaluation.get("dimensions") or []),
        ]
    )

    process_eval_body = "".join(
        [
            '<div class="cmp-score-hero">'
            f"{_render_metric('总分', process_overall_text, _normalize_text(consultation_process_evaluation.get('overall_summary')) or None)}"
            f"{_render_metric('明细', process_total_text)}"
            "</div>",
            _render_process_sections(consultation_process_evaluation.get("sections") or []),
        ]
    )

    return (
        f'<article class="cmp-provider cmp-provider--{accent}">'
        '<div class="cmp-provider__head">'
        '<div>'
        f'<span class="cmp-provider__eyebrow">{html.escape(label)}</span>'
        f'<h3 class="cmp-provider__title">{html.escape(provider_name)}</h3>'
        "</div>"
        f'<span class="cmp-provider__speaker">{html.escape(_speaker_counter_text(utterances))}</span>'
        "</div>"
        f'<div class="cmp-provider__metrics">{overview_metrics}</div>'
        '<div class="cmp-provider__hero">'
        f'<span class="cmp-provider__status">{html.escape(_normalize_text(deal_outcome.get("status")) or "未明确")}</span>'
        f'<p class="cmp-provider__hero-text">{html.escape(hero_summary)}</p>'
        "</div>"
        f"{_render_fold('主诉与适应症', result_body, subtitle='尽量贴近现有录音详情页结构', open=True)}"
        f"{_render_fold('顾虑与客户画像', concern_body, open=True)}"
        f"{_render_fold('推荐方案与结果判断', recommendation_body, open=True)}"
        f"{_render_fold('咨询评价', consultation_eval_body)}"
        f"{_render_fold('面诊过程评价', process_eval_body)}"
        "</article>"
    )


def _speaker_kind(item: dict[str, Any]) -> str:
    speaker = _normalize_text(item.get("speaker") or item.get("speaker_role") or item.get("speaker_id")).lower()
    if speaker in {"customer", "patient", "client", "客户", "患者"}:
        return "customer"
    if speaker in {"consultant", "advisor", "咨询师"}:
        return "consultant"
    if speaker in {"doctor", "医生"}:
        return "doctor"
    return "neutral"


def _speaker_display(item: dict[str, Any]) -> str:
    for key in ("speaker_display_label", "speaker", "speaker_role", "speaker_id"):
        value = _normalize_text(item.get(key))
        if value:
            return value
    return "未知角色"


def _render_transcript_panel(
    *,
    title: str,
    accent: str,
    utterances: list[dict[str, Any]],
    duration_ms: int | None,
    full_text: str,
) -> str:
    if not utterances:
        transcript_html = _render_empty("当前没有可展示的对话全文")
    else:
        bubbles: list[str] = []
        for index, item in enumerate(utterances):
            kind = _speaker_kind(item)
            begin_ms = int(item.get("begin_ms") or 0)
            end_ms = int(item.get("end_ms") or begin_ms)
            bubble_class = f"cmp-bubble cmp-bubble--{kind}"
            if kind == "customer":
                bubble_class += " cmp-bubble--right"
            bubbles.append(
                f'<div class="{bubble_class}" data-index="{index}">'
                '<div class="cmp-bubble__head">'
                f'<span class="cmp-bubble__speaker">{html.escape(_speaker_display(item))}</span>'
                f'<span class="cmp-bubble__time">{html.escape(_format_ms(begin_ms))} - {html.escape(_format_ms(end_ms))}</span>'
                "</div>"
                f'<p class="cmp-bubble__text">{html.escape(_normalize_text(item.get("text")) or "（空白）")}</p>'
                "</div>"
            )
        transcript_html = f'<div class="cmp-transcript">{ "".join(bubbles) }</div>'

    return (
        f'<article class="cmp-provider cmp-provider--{accent} cmp-provider--transcript">'
        '<div class="cmp-provider__head">'
        '<div>'
        '<span class="cmp-provider__eyebrow">对话全文</span>'
        f'<h3 class="cmp-provider__title">{html.escape(title)}</h3>'
        "</div>"
        f'<span class="cmp-provider__speaker">{html.escape(_speaker_counter_text(utterances))}</span>'
        "</div>"
        '<div class="cmp-provider__metrics">'
        f"{_render_metric('句子数', _format_number(len(utterances)))}"
        f"{_render_metric('文本长度', _format_number(len(full_text)))}"
        f"{_render_metric('时长', _format_duration(duration_ms))}"
        "</div>"
        f"{transcript_html}"
        "</article>"
    )


def _build_markdown_report(
    *,
    recording: Recording,
    audio_path: Path,
    current_transcript: Transcript,
    current_analysis: AnalysisTask | None,
    xfyun_utterances: list[dict[str, Any]],
    xfyun_full_text: str,
    xfyun_duration_ms: int,
    xfyun_result: dict[str, Any],
    paths: ComparisonPaths,
) -> str:
    current_utterances = list(current_transcript.utterances or [])
    current_result = _analysis_result_from_task(current_analysis)
    current_snapshot = _analysis_snapshot(current_result)
    xfyun_snapshot = _analysis_snapshot(xfyun_result)

    return f"""# 录音对比报告

- 录音ID：`{recording.id}`
- 原始文件名：`{recording.file_name}`
- 音频路径：`{audio_path}`
- 创建时间：`{_format_datetime(recording.created_at)}`
- 当前转写 Provider：`{current_transcript.asr_provider}`
- 当前分析任务：`{current_analysis.id if current_analysis else "无"}`
- 对比产出目录：`{paths.root}`

## 转写概览

| 维度 | 当前结果 | 讯飞旁路结果 |
| --- | --- | --- |
| utterance 数 | {len(current_utterances)} | {len(xfyun_utterances)} |
| duration_ms | {current_transcript.duration_ms or 0} | {xfyun_duration_ms} |
| 文本长度 | {len(current_transcript.full_text or "")} | {len(xfyun_full_text)} |
| speaker 分布 | `{json.dumps(_speaker_counter(current_utterances), ensure_ascii=False)}` | `{json.dumps(_speaker_counter(xfyun_utterances), ensure_ascii=False)}` |

## 分析概览

### 当前结果

```json
{json.dumps(current_snapshot, ensure_ascii=False, indent=2)}
```

### 讯飞结果

```json
{json.dumps(xfyun_snapshot, ensure_ascii=False, indent=2)}
```

## 产物文件

- 当前转写 JSON：`{paths.current_transcript_json}`
- 当前转写文本：`{paths.current_transcript_text}`
- 当前分析 JSON：`{paths.current_analysis_json}`
- 讯飞转写 JSON：`{paths.xfyun_transcript_json}`
- 讯飞转写文本：`{paths.xfyun_transcript_text}`
- 讯飞分析输入：`{paths.xfyun_analysis_input_json}`
- 讯飞分析结果：`{paths.xfyun_analysis_json}`
- 转写 diff：`{paths.transcript_diff}`
- 分析 diff：`{paths.analysis_diff}`
"""


def _build_html_report(
    *,
    compare_root: Path,
    recording: Recording,
    audio_path: Path,
    current_transcript: Transcript,
    current_analysis: AnalysisTask | None,
    xfyun_utterances: list[dict[str, Any]],
    xfyun_full_text: str,
    xfyun_duration_ms: int,
    xfyun_result: dict[str, Any],
    paths: ComparisonPaths,
    transcript_diff_text: str,
    analysis_diff_text: str,
) -> str:
    current_utterances = list(current_transcript.utterances or [])
    current_result = _analysis_result_from_task(current_analysis)

    current_status = _normalize_text(current_result.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "未明确"
    xfyun_status = _normalize_text(xfyun_result.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "未明确"

    current_indications = _extract_labels(current_result.get("standardized_indications", {}).get("items") or [], "indication_name")
    xfyun_indications = _extract_labels(xfyun_result.get("standardized_indications", {}).get("items") or [], "indication_name")
    indication_shared, indication_current_only, indication_xfyun_only = _list_diff(current_indications, xfyun_indications)

    current_concerns = _extract_labels(current_result.get("customer_concerns", {}).get("items") or [], "content")
    xfyun_concerns = _extract_labels(xfyun_result.get("customer_concerns", {}).get("items") or [], "content")
    concern_shared, concern_current_only, concern_xfyun_only = _list_diff(current_concerns, xfyun_concerns)

    current_plans = _extract_labels(current_result.get("consultation_result", {}).get("recommended_plan", {}).get("items") or [], "plan")
    xfyun_plans = _extract_labels(xfyun_result.get("consultation_result", {}).get("recommended_plan", {}).get("items") or [], "plan")
    plan_shared, plan_current_only, plan_xfyun_only = _list_diff(current_plans, xfyun_plans)

    current_profile_tags = _analysis_snapshot(current_result).get("customer_profile_tags", [])
    xfyun_profile_tags = _analysis_snapshot(xfyun_result).get("customer_profile_tags", [])
    profile_shared, profile_current_only, profile_xfyun_only = _list_diff(current_profile_tags, xfyun_profile_tags)

    audio_href = _relative_href(compare_root, audio_path)
    artifact_links_html = "".join(
        [
            _render_artifact_link("页面版报告", _relative_href(compare_root, Path(paths.report_html))),
            _render_artifact_link("Markdown 摘要", _relative_href(compare_root, Path(paths.report_markdown))),
            _render_artifact_link("当前转写 JSON", _relative_href(compare_root, Path(paths.current_transcript_json))),
            _render_artifact_link("当前分析 JSON", _relative_href(compare_root, Path(paths.current_analysis_json))),
            _render_artifact_link("讯飞转写 JSON", _relative_href(compare_root, Path(paths.xfyun_transcript_json))),
            _render_artifact_link("讯飞分析 JSON", _relative_href(compare_root, Path(paths.xfyun_analysis_json))),
            _render_artifact_link("转写 Diff", _relative_href(compare_root, Path(paths.transcript_diff))),
            _render_artifact_link("分析 Diff", _relative_href(compare_root, Path(paths.analysis_diff))),
        ]
    )

    change_cards_html = "".join(
        [
            _render_change_card(
                title="成交判断",
                left_label="当前结果",
                left_value=current_status,
                right_label="讯飞旁路",
                right_value=xfyun_status,
                body_html=_render_text_block(
                    _normalize_text(current_result.get("consultation_result", {}).get("deal_outcome", {}).get("summary"))
                    or _normalize_text(xfyun_result.get("consultation_result", {}).get("deal_outcome", {}).get("summary")),
                    class_name="cmp-change-card__summary",
                ),
            ),
            _render_change_card(
                title="适应症识别",
                left_label="当前结果",
                left_value=f"{len(_dedupe_strings(current_indications))} 项",
                right_label="讯飞旁路",
                right_value=f"{len(_dedupe_strings(xfyun_indications))} 项",
                body_html=_render_diff_groups(indication_shared, indication_current_only, indication_xfyun_only),
            ),
            _render_change_card(
                title="客户顾虑",
                left_label="当前结果",
                left_value=f"{len(_dedupe_strings(current_concerns))} 项",
                right_label="讯飞旁路",
                right_value=f"{len(_dedupe_strings(xfyun_concerns))} 项",
                body_html=_render_diff_groups(concern_shared, concern_current_only, concern_xfyun_only),
            ),
            _render_change_card(
                title="推荐方案",
                left_label="当前结果",
                left_value=f"{len(_dedupe_strings(current_plans))} 项",
                right_label="讯飞旁路",
                right_value=f"{len(_dedupe_strings(xfyun_plans))} 项",
                body_html=_render_diff_groups(plan_shared, plan_current_only, plan_xfyun_only),
            ),
            _render_change_card(
                title="画像标签",
                left_label="当前结果",
                left_value=f"{len(_dedupe_strings(current_profile_tags))} 个",
                right_label="讯飞旁路",
                right_value=f"{len(_dedupe_strings(xfyun_profile_tags))} 个",
                body_html=_render_diff_groups(profile_shared, profile_current_only, profile_xfyun_only),
            ),
            _render_change_card(
                title="对话样本",
                left_label="当前结果",
                left_value=f"{len(current_utterances)} 句 / {len(current_transcript.full_text or '')} 字",
                right_label="讯飞旁路",
                right_value=f"{len(xfyun_utterances)} 句 / {len(xfyun_full_text)} 字",
                body_html='<div class="cmp-inline-note">'
                f"{html.escape('当前：' + _speaker_counter_text(current_utterances))}"
                "<br />"
                f"{html.escape('讯飞：' + _speaker_counter_text(xfyun_utterances))}"
                "</div>",
            ),
        ]
    )

    styles = """
    :root {
      color-scheme: light;
      --cmp-bg: #eef4fb;
      --cmp-card: #ffffff;
      --cmp-text: #11213a;
      --cmp-muted: #66758f;
      --cmp-border: rgba(105, 133, 173, 0.18);
      --cmp-shadow: 0 18px 48px rgba(24, 53, 92, 0.08);
      --cmp-blue: #246bce;
      --cmp-blue-soft: #eaf3ff;
      --cmp-teal: #0e8b7b;
      --cmp-teal-soft: #e7faf6;
      --cmp-amber: #b76c12;
      --cmp-amber-soft: #fff3df;
      --cmp-slate-soft: #f3f6fb;
      --cmp-danger: #c44536;
      --cmp-danger-soft: #fff0ee;
      --cmp-radius-xl: 28px;
      --cmp-radius-lg: 22px;
      --cmp-radius-md: 16px;
      --cmp-radius-sm: 12px;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(171, 214, 255, 0.42), transparent 28%),
        linear-gradient(180deg, #f5f9ff 0%, var(--cmp-bg) 48%, #f7fbff 100%);
      color: var(--cmp-text);
      font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
    }

    a {
      color: inherit;
      text-decoration: none;
    }

    .cmp-page {
      max-width: 1520px;
      margin: 0 auto;
      padding: 28px 22px 40px;
    }

    .cmp-hero {
      position: relative;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(340px, 0.9fr);
      gap: 18px;
      padding: 26px 28px;
      border-radius: var(--cmp-radius-xl);
      border: 1px solid rgba(138, 182, 240, 0.32);
      background:
        linear-gradient(145deg, rgba(231, 245, 255, 0.96), rgba(255, 255, 255, 0.98)),
        linear-gradient(180deg, rgba(255,255,255,0.95), rgba(255,255,255,0.86));
      box-shadow: var(--cmp-shadow);
    }

    .cmp-hero::after {
      content: "";
      position: absolute;
      inset: auto -90px -120px auto;
      width: 260px;
      height: 260px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(83, 159, 255, 0.22), transparent 65%);
      pointer-events: none;
    }

    .cmp-hero__eyebrow {
      display: inline-flex;
      align-items: center;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(36, 107, 206, 0.1);
      color: var(--cmp-blue);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .cmp-hero h1 {
      margin: 14px 0 10px;
      font-size: clamp(30px, 4vw, 42px);
      line-height: 1.08;
    }

    .cmp-hero__subtitle {
      margin: 0;
      color: var(--cmp-muted);
      font-size: 15px;
      line-height: 1.7;
    }

    .cmp-meta-row,
    .cmp-pill-row,
    .cmp-link-grid,
    .cmp-change-grid,
    .cmp-compare-grid,
    .cmp-card-list,
    .cmp-provider__metrics,
    .cmp-eval-grid,
    .cmp-transcript {
      display: grid;
      gap: 12px;
    }

    .cmp-meta-row {
      margin-top: 18px;
      grid-template-columns: repeat(auto-fit, minmax(180px, max-content));
    }

    .cmp-pill-row {
      margin-top: 14px;
      grid-template-columns: repeat(auto-fit, minmax(170px, max-content));
    }

    .cmp-hero__aside {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .cmp-card,
    .cmp-provider,
    .cmp-change-card,
    .cmp-link-card,
    .cmp-metric,
    .cmp-item-card,
    .cmp-eval-card,
    .cmp-process-card {
      background: var(--cmp-card);
      border: 1px solid var(--cmp-border);
      box-shadow: var(--cmp-shadow);
    }

    .cmp-card {
      margin-top: 20px;
      padding: 24px;
      border-radius: var(--cmp-radius-lg);
    }

    .cmp-card__head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }

    .cmp-card__head h2 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
    }

    .cmp-card__head p {
      margin: 6px 0 0;
      color: var(--cmp-muted);
      font-size: 14px;
      line-height: 1.6;
    }

    .cmp-note {
      padding: 12px 14px;
      border-radius: var(--cmp-radius-sm);
      background: rgba(17, 33, 58, 0.05);
      color: var(--cmp-muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .cmp-metric {
      padding: 16px 18px;
      border-radius: var(--cmp-radius-md);
    }

    .cmp-metric label,
    .cmp-provider__eyebrow,
    .cmp-change-card__title,
    .cmp-link-card span,
    .cmp-process-section summary span,
    .cmp-fold summary small,
    .cmp-diff-group__label,
    .cmp-item-card__meta,
    .cmp-inline-note {
      color: var(--cmp-muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .cmp-metric strong {
      display: block;
      margin-top: 6px;
      font-size: 24px;
      line-height: 1.1;
    }

    .cmp-metric small {
      display: block;
      margin-top: 8px;
      color: var(--cmp-muted);
      line-height: 1.55;
    }

    .cmp-chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .cmp-chip {
      display: inline-flex;
      align-items: center;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 600;
      line-height: 1.3;
      border: 1px solid transparent;
      background: var(--cmp-slate-soft);
    }

    .cmp-chip--neutral {
      background: var(--cmp-slate-soft);
      color: var(--cmp-text);
    }

    .cmp-chip--current {
      background: var(--cmp-blue-soft);
      color: var(--cmp-blue);
      border-color: rgba(36, 107, 206, 0.16);
    }

    .cmp-chip--variant,
    .cmp-chip--accent {
      background: var(--cmp-teal-soft);
      color: var(--cmp-teal);
      border-color: rgba(14, 139, 123, 0.16);
    }

    .cmp-empty,
    .cmp-section-summary,
    .cmp-inline-summary,
    .cmp-item-card__summary,
    .cmp-eval-card__summary,
    .cmp-process-card__summary,
    .cmp-process-card__issue,
    .cmp-provider__hero-text {
      margin: 0;
      color: var(--cmp-muted);
      line-height: 1.7;
      font-size: 14px;
    }

    .cmp-empty {
      padding: 12px 14px;
      border-radius: var(--cmp-radius-sm);
      background: rgba(17, 33, 58, 0.04);
    }

    .cmp-link-grid,
    .cmp-change-grid,
    .cmp-compare-grid,
    .cmp-card-list,
    .cmp-provider__metrics,
    .cmp-eval-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .cmp-link-grid {
      margin-top: 18px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }

    .cmp-link-card {
      padding: 16px 18px;
      border-radius: var(--cmp-radius-md);
      transition: transform 120ms ease, border-color 120ms ease;
    }

    .cmp-link-card:hover {
      transform: translateY(-1px);
      border-color: rgba(36, 107, 206, 0.26);
    }

    .cmp-link-card strong {
      display: block;
      margin-bottom: 6px;
      font-size: 16px;
      line-height: 1.4;
    }

    .cmp-audio-shell {
      display: grid;
      gap: 12px;
      margin-top: 8px;
    }

    .cmp-audio-shell audio {
      width: 100%;
      min-height: 58px;
      border-radius: 999px;
      background: rgba(236, 244, 255, 0.92);
    }

    .cmp-change-card {
      padding: 18px;
      border-radius: var(--cmp-radius-md);
    }

    .cmp-change-card__title {
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .cmp-change-card__compare {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
      margin-bottom: 12px;
    }

    .cmp-change-card__cell {
      padding: 14px;
      border-radius: var(--cmp-radius-sm);
    }

    .cmp-change-card__cell--current {
      background: var(--cmp-blue-soft);
    }

    .cmp-change-card__cell--variant {
      background: var(--cmp-teal-soft);
    }

    .cmp-change-card__cell small {
      display: block;
      color: var(--cmp-muted);
      font-size: 12px;
    }

    .cmp-change-card__cell strong {
      display: block;
      margin-top: 6px;
      font-size: 20px;
      line-height: 1.25;
    }

    .cmp-change-card__summary {
      margin-top: 6px;
      margin-bottom: 10px;
    }

    .cmp-diff-group + .cmp-diff-group {
      margin-top: 10px;
    }

    .cmp-compare-grid {
      align-items: start;
    }

    .cmp-provider {
      padding: 22px;
      border-radius: var(--cmp-radius-lg);
      overflow: hidden;
    }

    .cmp-provider--current {
      border-top: 6px solid rgba(36, 107, 206, 0.28);
    }

    .cmp-provider--variant {
      border-top: 6px solid rgba(14, 139, 123, 0.28);
    }

    .cmp-provider--transcript {
      padding-bottom: 18px;
    }

    .cmp-provider__head {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
      margin-bottom: 16px;
    }

    .cmp-provider__title {
      margin: 5px 0 0;
      font-size: 24px;
      line-height: 1.15;
    }

    .cmp-provider__speaker {
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(17, 33, 58, 0.06);
      font-size: 12px;
      color: var(--cmp-muted);
    }

    .cmp-provider__hero {
      margin-top: 16px;
      margin-bottom: 16px;
      padding: 18px 20px;
      border-radius: var(--cmp-radius-md);
      background: linear-gradient(145deg, rgba(245, 249, 255, 0.96), rgba(251, 253, 255, 0.98));
      border: 1px solid rgba(105, 133, 173, 0.14);
    }

    .cmp-provider__status {
      display: inline-flex;
      align-items: center;
      margin-bottom: 10px;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(17, 33, 58, 0.07);
      font-size: 13px;
      font-weight: 700;
    }

    .cmp-result-pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }

    .cmp-fold,
    .cmp-process-section,
    .cmp-evidence {
      border: 1px solid rgba(105, 133, 173, 0.16);
      border-radius: var(--cmp-radius-md);
      background: rgba(255, 255, 255, 0.92);
    }

    .cmp-fold + .cmp-fold,
    .cmp-process-section + .cmp-process-section {
      margin-top: 12px;
    }

    .cmp-fold summary,
    .cmp-process-section summary,
    .cmp-evidence summary {
      list-style: none;
      cursor: pointer;
    }

    .cmp-fold summary::-webkit-details-marker,
    .cmp-process-section summary::-webkit-details-marker,
    .cmp-evidence summary::-webkit-details-marker {
      display: none;
    }

    .cmp-fold summary {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 16px 18px;
      font-size: 16px;
      font-weight: 700;
    }

    .cmp-fold__body,
    .cmp-process-section__body,
    .cmp-evidence__body {
      padding: 0 18px 18px;
    }

    .cmp-subsection + .cmp-subsection {
      margin-top: 18px;
    }

    .cmp-subsection h4,
    .cmp-profile-group__title {
      margin: 0 0 10px;
      font-size: 15px;
      line-height: 1.4;
    }

    .cmp-card-list {
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }

    .cmp-item-card,
    .cmp-eval-card,
    .cmp-process-card {
      padding: 14px 15px;
      border-radius: var(--cmp-radius-sm);
    }

    .cmp-item-card__title,
    .cmp-process-card__head strong,
    .cmp-eval-card__head strong {
      font-size: 15px;
      line-height: 1.45;
    }

    .cmp-item-card__meta {
      margin-top: 6px;
      margin-bottom: 8px;
    }

    .cmp-evidence {
      margin-top: 10px;
      border-radius: var(--cmp-radius-sm);
      background: rgba(17, 33, 58, 0.03);
      box-shadow: none;
    }

    .cmp-evidence summary {
      padding: 10px 12px;
      color: var(--cmp-blue);
      font-size: 13px;
      font-weight: 700;
    }

    .cmp-evidence__body p {
      margin: 0;
      color: var(--cmp-muted);
      line-height: 1.7;
      font-size: 13px;
    }

    .cmp-evidence__body p + p {
      margin-top: 8px;
    }

    .cmp-inline-note,
    .cmp-inline-summary {
      margin-bottom: 10px;
    }

    .cmp-score-hero {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .cmp-eval-grid {
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    }

    .cmp-eval-card__head,
    .cmp-process-card__head,
    .cmp-process-section summary {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
    }

    .cmp-eval-card__head span,
    .cmp-process-card__head span,
    .cmp-process-section summary em {
      color: var(--cmp-muted);
      font-size: 12px;
      font-style: normal;
    }

    .cmp-process-section summary {
      padding: 14px 16px;
    }

    .cmp-process-section__list {
      display: grid;
      gap: 10px;
    }

    .cmp-score-line + .cmp-score-line {
      margin-top: 10px;
    }

    .cmp-score-line__row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 7px;
      font-size: 13px;
      color: var(--cmp-muted);
    }

    .cmp-score-line__track {
      height: 8px;
      border-radius: 999px;
      background: rgba(17, 33, 58, 0.08);
      overflow: hidden;
    }

    .cmp-score-line__track span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(36, 107, 206, 0.86), rgba(14, 139, 123, 0.82));
    }

    .cmp-transcript {
      margin-top: 16px;
      max-height: 760px;
      overflow: auto;
      padding-right: 6px;
      grid-template-columns: 1fr;
    }

    .cmp-bubble {
      max-width: 92%;
      padding: 14px 16px;
      border-radius: 18px 18px 18px 8px;
      background: #f4f8ff;
      border: 1px solid rgba(36, 107, 206, 0.14);
    }

    .cmp-bubble--consultant {
      background: #edf5ff;
    }

    .cmp-bubble--doctor {
      background: #fff4e3;
      border-color: rgba(183, 108, 18, 0.18);
    }

    .cmp-bubble--customer {
      margin-left: auto;
      border-radius: 18px 18px 8px 18px;
      background: #ecfbf8;
      border-color: rgba(14, 139, 123, 0.16);
    }

    .cmp-bubble--neutral {
      background: #f5f7fb;
    }

    .cmp-bubble__head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--cmp-muted);
    }

    .cmp-bubble__speaker {
      font-weight: 700;
      color: var(--cmp-text);
    }

    .cmp-bubble__text {
      margin: 0;
      font-size: 14px;
      line-height: 1.72;
      white-space: pre-wrap;
      word-break: break-word;
    }

    pre {
      margin: 0;
      padding: 16px 18px;
      border-radius: var(--cmp-radius-md);
      background: #0f1e32;
      color: #d9e5f7;
      line-height: 1.7;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "SFMono-Regular", "Consolas", monospace;
      font-size: 12px;
    }

    @media (max-width: 1180px) {
      .cmp-hero,
      .cmp-compare-grid,
      .cmp-change-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 820px) {
      .cmp-page {
        padding: 16px 14px 28px;
      }

      .cmp-card,
      .cmp-provider {
        padding: 18px;
      }

      .cmp-link-grid,
      .cmp-card-list,
      .cmp-eval-grid,
      .cmp-provider__metrics,
      .cmp-score-hero,
      .cmp-change-card__compare {
        grid-template-columns: 1fr;
      }

      .cmp-meta-row,
      .cmp-pill-row {
        grid-template-columns: 1fr;
      }

      .cmp-provider__head,
      .cmp-card__head {
        flex-direction: column;
        align-items: stretch;
      }

      .cmp-bubble {
        max-width: 100%;
      }
    }
    """

    hero_meta = "".join(
        [
            _render_chip(f"录音ID {recording.id}", tone="neutral"),
            _render_chip(f"当前转写 {current_transcript.asr_provider}", tone="current"),
            _render_chip("旁路转写 xfyun_asr", tone="variant"),
            _render_chip(f"原始文件 {recording.file_name}", tone="neutral"),
        ]
    )
    hero_aside = "".join(
        [
            _render_metric("录音时长", _format_duration(current_transcript.duration_ms or xfyun_duration_ms)),
            _render_metric("创建时间", _format_datetime(recording.created_at)),
            _render_metric("当前分析任务", current_analysis.id if current_analysis else "无"),
            _render_metric("当前页面", "仅比较文件，不覆盖数据库", "原始 transcript / analysis 仍然保持不变"),
        ]
    )

    html_parts = [
        "<!doctype html>",
        '<html lang="zh-CN">',
        "<head>",
        '  <meta charset="utf-8" />',
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />',
        "  <title>录音对比报告</title>",
        f"  <style>{styles}</style>",
        "</head>",
        "<body>",
        '  <main class="cmp-page">',
        '    <header class="cmp-hero">',
        '      <section class="cmp-hero__main">',
        '        <span class="cmp-hero__eyebrow">录音分析结果详情 · 对比视图</span>',
        f"        <h1>{html.escape(recording.file_name)}</h1>",
        '        <p class="cmp-hero__subtitle">这份页面沿用现有录音分析详情页的卡片式信息结构，把“当前腾讯结果”和“讯飞旁路结果”并排展开，方便直接看主诉、顾虑、推荐方案、评分与全文差异。</p>',
        f'        <div class="cmp-pill-row">{hero_meta}</div>',
        f'        <div class="cmp-meta-row">{_render_chip(f"当前成交判断：{current_status}", tone="current")}{_render_chip(f"讯飞成交判断：{xfyun_status}", tone="variant")}</div>',
        "      </section>",
        f'      <aside class="cmp-hero__aside">{hero_aside}</aside>',
        "    </header>",
        '    <section class="cmp-card">',
        '      <div class="cmp-card__head">',
        "        <div>",
        "          <h2>音频与产物</h2>",
        "          <p>上面是可直接试听的原始录音，下面保留本次对比的 JSON、Diff 和 Markdown 文件。</p>",
        "        </div>",
        '        <div class="cmp-note">这里展示的是旁路产物，数据库里的原始转写和分析任务没有被覆盖。</div>',
        "      </div>",
        '      <div class="cmp-audio-shell">',
        f'        <audio controls preload="metadata" src="{html.escape(audio_href, quote=True)}">您的浏览器暂不支持音频播放。</audio>',
        "      </div>",
        f'      <div class="cmp-link-grid">{artifact_links_html}</div>',
        "    </section>",
        '    <section class="cmp-card">',
        '      <div class="cmp-card__head">',
        "        <div>",
        "          <h2>关键差异</h2>",
        "          <p>先快速看结论，再往下看两套结果的完整详情与全文对照。</p>",
        "        </div>",
        "      </div>",
        f'      <div class="cmp-change-grid">{change_cards_html}</div>',
        "    </section>",
        '    <section class="cmp-card">',
        '      <div class="cmp-card__head">',
        "        <div>",
        "          <h2>分析结果</h2>",
        "          <p>信息组织尽量贴近当前前端录音分析详情页：先看综合结论，再按主诉、顾虑、推荐、评分和过程评价展开。</p>",
        "        </div>",
        "      </div>",
        '      <div class="cmp-compare-grid">',
        _render_provider_panel(
            label="当前结果",
            provider_name=current_transcript.asr_provider,
            accent="current",
            utterances=current_utterances,
            full_text=current_transcript.full_text or "",
            duration_ms=current_transcript.duration_ms,
            result=current_result,
        ),
        _render_provider_panel(
            label="讯飞旁路",
            provider_name="xfyun_asr",
            accent="variant",
            utterances=xfyun_utterances,
            full_text=xfyun_full_text,
            duration_ms=xfyun_duration_ms,
            result=xfyun_result,
        ),
        "      </div>",
        "    </section>",
        '    <section class="cmp-card">',
        '      <div class="cmp-card__head">',
        "        <div>",
        "          <h2>对话全文</h2>",
        "          <p>左边是当前系统记录，右边是讯飞旁路结果。两边都保留角色标签、时间戳和全文内容。</p>",
        "        </div>",
        "      </div>",
        '      <div class="cmp-compare-grid">',
        _render_transcript_panel(
            title="当前结果",
            accent="current",
            utterances=current_utterances,
            duration_ms=current_transcript.duration_ms,
            full_text=current_transcript.full_text or "",
        ),
        _render_transcript_panel(
            title="讯飞旁路",
            accent="variant",
            utterances=xfyun_utterances,
            duration_ms=xfyun_duration_ms,
            full_text=xfyun_full_text,
        ),
        "      </div>",
        "    </section>",
        '    <section class="cmp-card">',
        '      <div class="cmp-card__head">',
        "        <div>",
        "          <h2>原始 Diff</h2>",
        "          <p>如果需要逐行核查，可以继续看文本 diff。上面几块更适合业务对比，这里更适合工程排查。</p>",
        "        </div>",
        "      </div>",
        '      <div class="cmp-compare-grid">',
        '        <article class="cmp-provider cmp-provider--current cmp-provider--transcript">',
        '          <div class="cmp-provider__head"><div><span class="cmp-provider__eyebrow">文本差异</span><h3 class="cmp-provider__title">转写 Diff</h3></div></div>',
        f"          <pre>{html.escape(transcript_diff_text)}</pre>",
        "        </article>",
        '        <article class="cmp-provider cmp-provider--variant cmp-provider--transcript">',
        '          <div class="cmp-provider__head"><div><span class="cmp-provider__eyebrow">结构差异</span><h3 class="cmp-provider__title">分析 Diff</h3></div></div>',
        f"          <pre>{html.escape(analysis_diff_text)}</pre>",
        "        </article>",
        "      </div>",
        "    </section>",
        "  </main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(html_parts)


async def _load_recording(recording_id: str) -> tuple[Recording, Transcript, AnalysisTask | None]:
    async with _session_factory() as db:
        recording = (
            await db.execute(
                select(Recording)
                .where(Recording.id == recording_id)
                .options(selectinload(Recording.staff), selectinload(Recording.transcript))
            )
        ).scalars().first()
        if recording is None:
            raise SystemExit(f"Recording not found: {recording_id}")
        transcript = recording.transcript
        if transcript is None or transcript.status != "completed":
            raise SystemExit(f"Current transcript is not ready for recording {recording_id}")
        current_analysis = (
            await db.execute(
                select(AnalysisTask)
                .where(AnalysisTask.file_name == f"recording_{recording_id}.json")
                .order_by(AnalysisTask.completed_at.desc())
            )
        ).scalars().first()
        return recording, transcript, current_analysis


async def _build_system_prompt() -> str:
    async with _session_factory() as db:
        return await build_system_prompt(db)


async def _transcribe_with_xfyun_without_side_effects(recording: Recording, audio_path: Path) -> tuple[list[dict[str, Any]], str, int]:
    raw_utterances, full_text, duration_ms = await transcribe_audio_with_xfyun(audio_path)
    utterances, correction_count = apply_medical_aesthetic_term_normalization(raw_utterances)
    if correction_count:
        full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()

    staff = recording.staff
    utterances = resolve_speaker_roles(
        utterances,
        staff_id=recording.staff_id,
        staff_name=staff.name if staff else None,
        staff_role=staff.role if staff else None,
    )
    utterances = apply_staff_voiceprints(audio_path, utterances, staff_id=recording.staff_id)
    utterances = resolve_speaker_roles(
        utterances,
        staff_id=recording.staff_id,
        staff_name=staff.name if staff else None,
        staff_role=staff.role if staff else None,
    )
    resolved_full_text = " ".join(str(item.get("text") or "") for item in utterances).strip()
    resolved_duration_ms = utterances[-1]["end_ms"] if utterances else duration_ms
    return utterances, resolved_full_text or full_text, resolved_duration_ms


async def main() -> None:
    parser = argparse.ArgumentParser(description="Use XFYUN ASR to create a side-by-side comparison for one recording.")
    parser.add_argument("--recording-id", required=True, help="Recording.id to compare")
    args = parser.parse_args()

    settings = get_settings()
    recording, current_transcript, current_analysis = await _load_recording(args.recording_id)
    audio_path = settings.resolve_file_path(recording.file_path)
    if not audio_path.is_file():
        raise SystemExit(f"Audio file not found: {audio_path}")

    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    compare_root = (settings.upload_path / "comparisons" / f"recording_{recording.id}_xfyun_{timestamp}").resolve()
    compare_root.mkdir(parents=True, exist_ok=True)

    current_transcript_json = compare_root / "current_transcript.json"
    current_transcript_text = compare_root / "current_transcript.txt"
    current_analysis_json = compare_root / "current_analysis.json"
    xfyun_transcript_json = compare_root / "xfyun_transcript.json"
    xfyun_transcript_text = compare_root / "xfyun_transcript.txt"
    xfyun_analysis_input_json = compare_root / "xfyun_analysis_input.json"
    xfyun_analysis_json = compare_root / "xfyun_analysis.json"
    transcript_diff = compare_root / "transcript.diff"
    analysis_diff = compare_root / "analysis.diff"
    metadata_json = compare_root / "metadata.json"
    report_markdown = compare_root / "compare_report.md"
    report_html = compare_root / "compare_report.html"

    current_utterances = list(current_transcript.utterances or [])
    _serialize_json(
        {
            "recording_id": recording.id,
            "file_name": recording.file_name,
            "file_path": recording.file_path,
            "created_at": str(recording.created_at),
            "current_transcript": {
                "id": current_transcript.id,
                "provider": current_transcript.asr_provider,
                "status": current_transcript.status,
                "duration_ms": current_transcript.duration_ms,
                "utterances": current_utterances,
                "full_text": current_transcript.full_text,
            },
            "current_analysis_task": {
                "id": current_analysis.id if current_analysis else None,
                "status": current_analysis.status if current_analysis else None,
                "completed_at": str(current_analysis.completed_at) if current_analysis else None,
            },
        },
        metadata_json,
    )
    _serialize_json(
        {
            "id": current_transcript.id,
            "provider": current_transcript.asr_provider,
            "status": current_transcript.status,
            "duration_ms": current_transcript.duration_ms,
            "utterances": current_utterances,
            "full_text": current_transcript.full_text,
        },
        current_transcript_json,
    )
    _write_text(current_transcript_text, _utterance_lines(current_utterances))
    _serialize_json(_analysis_result_from_task(current_analysis), current_analysis_json)

    xfyun_utterances, xfyun_full_text, xfyun_duration_ms = await _transcribe_with_xfyun_without_side_effects(recording, audio_path)
    xfyun_transcript_payload = {
        "provider": "xfyun_asr",
        "duration_ms": xfyun_duration_ms,
        "utterances": xfyun_utterances,
        "full_text": xfyun_full_text,
    }
    _serialize_json(xfyun_transcript_payload, xfyun_transcript_json)
    _write_text(xfyun_transcript_text, _utterance_lines(xfyun_utterances))

    analysis_input_payload, _, _ = build_analysis_payload_from_utterances(xfyun_utterances)
    _serialize_json(analysis_input_payload, xfyun_analysis_input_json)

    system_prompt = await _build_system_prompt()
    xfyun_analysis_result = analyze_transcript(xfyun_analysis_input_json, system_prompt=system_prompt).model_dump()
    _serialize_json(xfyun_analysis_result, xfyun_analysis_json)

    _write_diff(
        transcript_diff,
        current_transcript_text.read_text(encoding="utf-8").splitlines(),
        xfyun_transcript_text.read_text(encoding="utf-8").splitlines(),
        fromfile="current_tencent_transcript",
        tofile="xfyun_transcript",
    )
    _write_diff(
        analysis_diff,
        current_analysis_json.read_text(encoding="utf-8").splitlines(),
        xfyun_analysis_json.read_text(encoding="utf-8").splitlines(),
        fromfile="current_analysis",
        tofile="xfyun_analysis",
    )

    paths = ComparisonPaths(
        root=str(compare_root),
        metadata=str(metadata_json),
        current_transcript_json=str(current_transcript_json),
        current_transcript_text=str(current_transcript_text),
        current_analysis_json=str(current_analysis_json),
        xfyun_transcript_json=str(xfyun_transcript_json),
        xfyun_transcript_text=str(xfyun_transcript_text),
        xfyun_analysis_input_json=str(xfyun_analysis_input_json),
        xfyun_analysis_json=str(xfyun_analysis_json),
        transcript_diff=str(transcript_diff),
        analysis_diff=str(analysis_diff),
        report_markdown=str(report_markdown),
        report_html=str(report_html),
    )
    markdown_report = _build_markdown_report(
        recording=recording,
        audio_path=audio_path,
        current_transcript=current_transcript,
        current_analysis=current_analysis,
        xfyun_utterances=xfyun_utterances,
        xfyun_full_text=xfyun_full_text,
        xfyun_duration_ms=xfyun_duration_ms,
        xfyun_result=xfyun_analysis_result,
        paths=paths,
    )
    report_markdown.write_text(markdown_report, encoding="utf-8")
    report_html.write_text(
        _build_html_report(
            compare_root=compare_root,
            recording=recording,
            audio_path=audio_path,
            current_transcript=current_transcript,
            current_analysis=current_analysis,
            xfyun_utterances=xfyun_utterances,
            xfyun_full_text=xfyun_full_text,
            xfyun_duration_ms=xfyun_duration_ms,
            xfyun_result=xfyun_analysis_result,
            paths=paths,
            transcript_diff_text=transcript_diff.read_text(encoding="utf-8"),
            analysis_diff_text=analysis_diff.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )

    print(json.dumps(asdict(paths), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
