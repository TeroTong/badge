from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import compare_recording_with_xfyun as cmp

from smart_badge_api.core.config import get_settings


@dataclass(slots=True)
class VariantBundle:
    label: str
    provider_name: str
    accent: str
    transcript: dict[str, Any]
    analysis: dict[str, Any]

    @property
    def utterances(self) -> list[dict[str, Any]]:
        return list(self.transcript.get("utterances") or [])

    @property
    def full_text(self) -> str:
        return str(self.transcript.get("full_text") or "")

    @property
    def duration_ms(self) -> int | None:
        value = self.transcript.get("duration_ms")
        return int(value) if isinstance(value, (int, float)) else None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _variant_from_files(*, label: str, provider_name: str, accent: str, transcript_path: Path, analysis_path: Path) -> VariantBundle:
    return VariantBundle(
        label=label,
        provider_name=provider_name,
        accent=accent,
        transcript=_load_json(transcript_path),
        analysis=_load_json(analysis_path),
    )


def _speaker_counter(utterances: Sequence[dict[str, Any]]) -> Counter[str]:
    return Counter(str(item.get("speaker") or item.get("speaker_id") or "unknown") for item in utterances)


def _speaker_bar(utterances: Sequence[dict[str, Any]]) -> str:
    counts = _speaker_counter(utterances)
    total = sum(counts.values())
    if total <= 0:
        return '<div class="rx-speaker-bar rx-speaker-bar--empty"><span>暂无角色分布</span></div>'

    tone_map = {
        "consultant": "consultant",
        "customer": "customer",
        "doctor": "doctor",
    }
    segments: list[str] = []
    legends: list[str] = []
    for speaker, count in counts.items():
        tone = tone_map.get(speaker, "neutral")
        width = max(count / total * 100.0, 4.0)
        segments.append(
            f'<span class="rx-speaker-bar__segment rx-speaker-bar__segment--{tone}" style="width:{width:.2f}%"></span>'
        )
        legends.append(
            f'<span class="rx-speaker-bar__legend rx-speaker-bar__legend--{tone}">{speaker} {count}</span>'
        )
    return (
        '<div class="rx-speaker-bar">'
        f'<div class="rx-speaker-bar__track">{"".join(segments)}</div>'
        f'<div class="rx-speaker-bar__legend-row">{"".join(legends)}</div>'
        "</div>"
    )


def _render_artifact_link(base_dir: Path, label: str, target: Path) -> str:
    href = cmp._relative_href(base_dir, target)
    return (
        '<a class="rx-link-card" href="'
        f'{href}">'
        f"<strong>{label}</strong>"
        f"<span>{target.name}</span>"
        "</a>"
    )


def _render_summary_card(variant: VariantBundle) -> str:
    deal_status = cmp._normalize_text(variant.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "未明确"
    hero_summary = (
        cmp._normalize_text(variant.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("summary"))
        or cmp._normalize_text(variant.analysis.get("consultation_evaluation", {}).get("overall_summary"))
        or "当前没有整理出明确综合结论"
    )
    consultation_score = variant.analysis.get("consultation_evaluation", {}).get("overall_score")
    process_score = variant.analysis.get("consultation_process_evaluation", {}).get("overall_score")

    metrics = "".join(
        [
            cmp._render_metric("句子数", cmp._format_number(len(variant.utterances))),
            cmp._render_metric("时长", cmp._format_duration(variant.duration_ms)),
            cmp._render_metric("咨询评分", f"{cmp._format_number(consultation_score)} 分"),
            cmp._render_metric("过程评分", f"{cmp._format_number(process_score)} 分"),
        ]
    )

    return (
        f'<article class="rx-summary-card rx-summary-card--{variant.accent}">'
        '<div class="rx-summary-card__head">'
        '<div>'
        f'<span class="rx-summary-card__eyebrow">{variant.label}</span>'
        f'<h3 class="rx-summary-card__title">{variant.provider_name}</h3>'
        "</div>"
        f"{cmp._render_chip(deal_status, tone='accent' if variant.accent == 'variant' else 'current')}"
        "</div>"
        f'<p class="rx-summary-card__summary">{hero_summary}</p>'
        f"{_speaker_bar(variant.utterances)}"
        f'<div class="rx-summary-card__metrics">{metrics}</div>'
        "</article>"
    )


def _render_matrix_table(current: VariantBundle, variant: VariantBundle) -> str:
    def labels(result: dict[str, Any], *path: str, item_keys: Sequence[str]) -> str:
        node: Any = result
        for key in path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        items = node.get("items") if isinstance(node, dict) else []
        if not isinstance(items, list):
            return "-"
        return "、".join(cmp._extract_labels(items, *item_keys)[:6]) or "-"

    rows = [
        ("说话人分布", cmp._speaker_counter_text(current.utterances), cmp._speaker_counter_text(variant.utterances)),
        ("客户发言数", cmp._format_number(_speaker_counter(current.utterances).get("customer", 0)), cmp._format_number(_speaker_counter(variant.utterances).get("customer", 0))),
        ("成交判断", cmp._normalize_text(current.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "-", cmp._normalize_text(variant.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "-"),
        ("主诉", labels(current.analysis, "customer_primary_demands", item_keys=("demand",)), labels(variant.analysis, "customer_primary_demands", item_keys=("demand",))),
        ("标准化适应症", labels(current.analysis, "standardized_indications", item_keys=("indication_name",)), labels(variant.analysis, "standardized_indications", item_keys=("indication_name",))),
        ("客户顾虑", labels(current.analysis, "customer_concerns", item_keys=("content",)), labels(variant.analysis, "customer_concerns", item_keys=("content",))),
        ("推荐方案", labels(current.analysis, "consultation_result", "recommended_plan", item_keys=("plan", "recommendation")), labels(variant.analysis, "consultation_result", "recommended_plan", item_keys=("plan", "recommendation"))),
        (
            "画像标签数",
            cmp._format_number(current.analysis.get("consultation_result", {}).get("customer_profile_summary", {}).get("extracted_tag_count")),
            cmp._format_number(variant.analysis.get("consultation_result", {}).get("customer_profile_summary", {}).get("extracted_tag_count")),
        ),
        (
            "咨询评分",
            f"{cmp._format_number(current.analysis.get('consultation_evaluation', {}).get('overall_score'))} 分",
            f"{cmp._format_number(variant.analysis.get('consultation_evaluation', {}).get('overall_score'))} 分",
        ),
        (
            "过程评分",
            f"{cmp._format_number(current.analysis.get('consultation_process_evaluation', {}).get('overall_score'))} 分",
            f"{cmp._format_number(variant.analysis.get('consultation_process_evaluation', {}).get('overall_score'))} 分",
        ),
    ]

    body = "".join(
        "<tr>"
        f"<th>{label}</th>"
        f"<td>{left}</td>"
        f"<td>{right}</td>"
        "</tr>"
        for label, left, right in rows
    )
    return (
        '<div class="rx-table-wrap">'
        '<table class="rx-table">'
        "<thead><tr><th>对比项</th><th>当前腾讯</th><th>讯飞 roleType=1</th></tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
        "</div>"
    )


def _render_key_change_section(current: VariantBundle, variant: VariantBundle) -> str:
    current_indications = cmp._extract_labels(current.analysis.get("standardized_indications", {}).get("items") or [], "indication_name")
    variant_indications = cmp._extract_labels(variant.analysis.get("standardized_indications", {}).get("items") or [], "indication_name")
    indication_shared, indication_current_only, indication_variant_only = cmp._list_diff(current_indications, variant_indications)

    current_concerns = cmp._extract_labels(current.analysis.get("customer_concerns", {}).get("items") or [], "content")
    variant_concerns = cmp._extract_labels(variant.analysis.get("customer_concerns", {}).get("items") or [], "content")
    concern_shared, concern_current_only, concern_variant_only = cmp._list_diff(current_concerns, variant_concerns)

    current_plans = cmp._extract_labels(current.analysis.get("consultation_result", {}).get("recommended_plan", {}).get("items") or [], "plan")
    variant_plans = cmp._extract_labels(variant.analysis.get("consultation_result", {}).get("recommended_plan", {}).get("items") or [], "plan")
    plan_shared, plan_current_only, plan_variant_only = cmp._list_diff(current_plans, variant_plans)

    current_customer = _speaker_counter(current.utterances).get("customer", 0)
    variant_customer = _speaker_counter(variant.utterances).get("customer", 0)

    cards = [
        cmp._render_change_card(
            title="角色分离",
            left_label="当前腾讯",
            left_value=f"客户 {current_customer} 句",
            right_label="讯飞 roleType=1",
            right_value=f"客户 {variant_customer} 句",
            body_html=(
                f'<p class="cmp-change-card__summary">讯飞 `roleType=1` 已经不再是单一角色转写，但客户发言量仍明显少于当前腾讯结果。</p>'
            ),
        ),
        cmp._render_change_card(
            title="成交判断",
            left_label="当前腾讯",
            left_value=cmp._normalize_text(current.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "未明确",
            right_label="讯飞 roleType=1",
            right_value=cmp._normalize_text(variant.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("status")) or "未明确",
            body_html=cmp._render_text_block(
                cmp._normalize_text(variant.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("summary"))
                or cmp._normalize_text(current.analysis.get("consultation_result", {}).get("deal_outcome", {}).get("summary")),
                class_name="cmp-change-card__summary",
            ),
        ),
        cmp._render_change_card(
            title="适应症识别",
            left_label="当前腾讯",
            left_value=f"{len(cmp._dedupe_strings(current_indications))} 项",
            right_label="讯飞 roleType=1",
            right_value=f"{len(cmp._dedupe_strings(variant_indications))} 项",
            body_html=cmp._render_diff_groups(indication_shared, indication_current_only, indication_variant_only),
        ),
        cmp._render_change_card(
            title="客户顾虑",
            left_label="当前腾讯",
            left_value=f"{len(cmp._dedupe_strings(current_concerns))} 项",
            right_label="讯飞 roleType=1",
            right_value=f"{len(cmp._dedupe_strings(variant_concerns))} 项",
            body_html=cmp._render_diff_groups(concern_shared, concern_current_only, concern_variant_only),
        ),
        cmp._render_change_card(
            title="推荐方案",
            left_label="当前腾讯",
            left_value=f"{len(cmp._dedupe_strings(current_plans))} 项",
            right_label="讯飞 roleType=1",
            right_value=f"{len(cmp._dedupe_strings(variant_plans))} 项",
            body_html=cmp._render_diff_groups(plan_shared, plan_current_only, plan_variant_only),
        ),
    ]
    return "".join(cards)


def _render_layout(
    *,
    compare_dir: Path,
    recording_id: str,
    file_name: str,
    created_at: str,
    audio_href: str,
    current: VariantBundle,
    variant: VariantBundle,
    transcript_diff_text: str,
    analysis_diff_text: str,
) -> str:
    source_links = "".join(
        [
            _render_artifact_link(compare_dir, "当前转写", compare_dir / "current_transcript.json"),
            _render_artifact_link(compare_dir, "当前分析", compare_dir / "current_analysis.json"),
            _render_artifact_link(compare_dir, "讯飞转写", compare_dir / "xfyun_transcript.json"),
            _render_artifact_link(compare_dir, "讯飞分析", compare_dir / "xfyun_analysis.json"),
            _render_artifact_link(compare_dir, "转写 Diff", compare_dir / "transcript.diff"),
            _render_artifact_link(compare_dir, "分析 Diff", compare_dir / "analysis.diff"),
        ]
    )

    key_changes_html = _render_key_change_section(current, variant)
    summary_cards_html = "".join([_render_summary_card(current), _render_summary_card(variant)])
    matrix_table_html = _render_matrix_table(current, variant)

    styles = """
    :root {
      color-scheme: light;
      --rx-bg: #eef5ff;
      --rx-card: rgba(255, 255, 255, 0.95);
      --rx-text: #14233d;
      --rx-muted: #66748e;
      --rx-border: rgba(104, 132, 171, 0.18);
      --rx-shadow: 0 18px 48px rgba(18, 47, 84, 0.08);
      --rx-blue: #246bce;
      --rx-blue-soft: #eaf2ff;
      --rx-teal: #0f8f7f;
      --rx-teal-soft: #e8faf6;
      --rx-amber: #bf7a19;
      --rx-amber-soft: #fff2dd;
      --rx-slate: #eff4fb;
      --rx-radius-xl: 28px;
      --rx-radius-lg: 22px;
      --rx-radius-md: 16px;
      --rx-radius-sm: 12px;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--rx-text);
      font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(123, 186, 255, 0.30), transparent 26%),
        radial-gradient(circle at right center, rgba(73, 191, 170, 0.16), transparent 22%),
        linear-gradient(180deg, #f7fbff 0%, var(--rx-bg) 54%, #f5f9ff 100%);
    }

    a { color: inherit; text-decoration: none; }

    .rx-page {
      max-width: 1480px;
      margin: 0 auto;
      padding: 28px 22px 40px;
    }

    .rx-hero,
    .rx-section,
    .rx-summary-card,
    .rx-link-card {
      background: var(--rx-card);
      border: 1px solid var(--rx-border);
      box-shadow: var(--rx-shadow);
    }

    .rx-hero {
      position: relative;
      overflow: hidden;
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(340px, 0.85fr);
      gap: 18px;
      padding: 28px;
      border-radius: var(--rx-radius-xl);
    }

    .rx-hero::before {
      content: "";
      position: absolute;
      inset: auto -60px -90px auto;
      width: 240px;
      height: 240px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(36, 107, 206, 0.16), transparent 68%);
      pointer-events: none;
    }

    .rx-hero__eyebrow {
      display: inline-flex;
      align-items: center;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(36, 107, 206, 0.10);
      color: var(--rx-blue);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .rx-hero__title {
      margin: 14px 0 12px;
      font-size: clamp(30px, 4vw, 42px);
      line-height: 1.08;
    }

    .rx-hero__subtitle {
      margin: 0;
      color: var(--rx-muted);
      font-size: 15px;
      line-height: 1.75;
    }

    .rx-pill-row,
    .rx-link-grid,
    .rx-summary-grid,
    .rx-change-grid,
    .rx-compare-grid {
      display: grid;
      gap: 14px;
    }

    .rx-pill-row {
      margin-top: 16px;
      grid-template-columns: repeat(auto-fit, minmax(170px, max-content));
    }

    .rx-hero__aside {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .rx-section {
      margin-top: 20px;
      padding: 24px;
      border-radius: var(--rx-radius-lg);
    }

    .rx-section__head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }

    .rx-section__head h2 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
    }

    .rx-section__head p {
      margin: 6px 0 0;
      color: var(--rx-muted);
      font-size: 14px;
      line-height: 1.65;
    }

    .rx-note {
      padding: 12px 14px;
      border-radius: var(--rx-radius-sm);
      background: rgba(20, 35, 61, 0.05);
      color: var(--rx-muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .rx-link-grid {
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    }

    .rx-link-card {
      padding: 16px 18px;
      border-radius: var(--rx-radius-md);
      transition: transform 120ms ease, border-color 120ms ease;
    }

    .rx-link-card:hover {
      transform: translateY(-1px);
      border-color: rgba(36, 107, 206, 0.28);
    }

    .rx-link-card strong {
      display: block;
      margin-bottom: 6px;
      font-size: 16px;
    }

    .rx-link-card span {
      color: var(--rx-muted);
      font-size: 13px;
    }

    .rx-summary-grid,
    .rx-compare-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .rx-summary-card {
      padding: 20px;
      border-radius: var(--rx-radius-lg);
      overflow: hidden;
    }

    .rx-summary-card--current { border-top: 6px solid rgba(36, 107, 206, 0.26); }
    .rx-summary-card--variant { border-top: 6px solid rgba(15, 143, 127, 0.26); }

    .rx-summary-card__head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 12px;
    }

    .rx-summary-card__eyebrow,
    .rx-speaker-bar__legend,
    .rx-speaker-bar--empty span,
    .cmp-change-card__title,
    .cmp-link-card span,
    .cmp-process-section summary span,
    .cmp-fold summary small,
    .cmp-diff-group__label,
    .cmp-item-card__meta,
    .cmp-inline-note,
    .cmp-metric label {
      color: var(--rx-muted);
      font-size: 13px;
      line-height: 1.5;
    }

    .rx-summary-card__title {
      margin: 4px 0 0;
      font-size: 24px;
      line-height: 1.15;
    }

    .rx-summary-card__summary {
      margin: 0 0 12px;
      color: var(--rx-muted);
      line-height: 1.7;
      min-height: 48px;
    }

    .rx-summary-card__metrics {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-top: 14px;
    }

    .rx-speaker-bar__track {
      display: flex;
      width: 100%;
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(20, 35, 61, 0.07);
    }

    .rx-speaker-bar__segment--consultant { background: #4b8fe8; }
    .rx-speaker-bar__segment--customer { background: #21a58f; }
    .rx-speaker-bar__segment--doctor { background: #d99839; }
    .rx-speaker-bar__segment--neutral { background: #9aabc3; }

    .rx-speaker-bar__legend-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .rx-speaker-bar__legend {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(20, 35, 61, 0.05);
    }

    .rx-speaker-bar__legend::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
      background: #9aabc3;
    }

    .rx-speaker-bar__legend--consultant::before { background: #4b8fe8; }
    .rx-speaker-bar__legend--customer::before { background: #21a58f; }
    .rx-speaker-bar__legend--doctor::before { background: #d99839; }

    .rx-change-grid { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }

    .rx-table-wrap {
      overflow: auto;
      border-radius: var(--rx-radius-md);
      border: 1px solid rgba(104, 132, 171, 0.14);
      background: rgba(255, 255, 255, 0.88);
    }

    .rx-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 780px;
    }

    .rx-table thead th {
      background: linear-gradient(180deg, rgba(242, 247, 255, 0.98), rgba(248, 251, 255, 0.98));
      color: var(--rx-text);
      font-size: 14px;
      font-weight: 700;
    }

    .rx-table th,
    .rx-table td {
      padding: 14px 16px;
      border-bottom: 1px solid rgba(104, 132, 171, 0.10);
      text-align: left;
      vertical-align: top;
      line-height: 1.65;
      font-size: 14px;
    }

    .rx-table tbody th {
      width: 160px;
      color: var(--rx-muted);
      font-weight: 600;
      background: rgba(247, 250, 255, 0.92);
      position: sticky;
      left: 0;
    }

    .cmp-provider,
    .cmp-change-card,
    .cmp-metric,
    .cmp-item-card,
    .cmp-eval-card,
    .cmp-process-card {
      background: rgba(255, 255, 255, 0.95);
      border: 1px solid rgba(104, 132, 171, 0.16);
      box-shadow: var(--rx-shadow);
    }

    .cmp-provider {
      padding: 22px;
      border-radius: var(--rx-radius-lg);
      overflow: hidden;
    }

    .cmp-provider--current { border-top: 6px solid rgba(36, 107, 206, 0.26); }
    .cmp-provider--variant { border-top: 6px solid rgba(15, 143, 127, 0.26); }
    .cmp-provider--transcript { padding-bottom: 18px; }

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
      background: rgba(20, 35, 61, 0.06);
      font-size: 12px;
      color: var(--rx-muted);
    }

    .cmp-provider__metrics,
    .cmp-card-list,
    .cmp-eval-grid,
    .cmp-score-hero {
      display: grid;
      gap: 12px;
    }

    .cmp-provider__metrics,
    .cmp-score-hero {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .cmp-provider__hero {
      margin-top: 16px;
      margin-bottom: 16px;
      padding: 18px 20px;
      border-radius: var(--rx-radius-md);
      background: linear-gradient(145deg, rgba(245, 249, 255, 0.96), rgba(251, 253, 255, 0.98));
      border: 1px solid rgba(104, 132, 171, 0.14);
    }

    .cmp-provider__status,
    .cmp-chip {
      display: inline-flex;
      align-items: center;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 700;
      line-height: 1.3;
    }

    .cmp-provider__status { margin-bottom: 10px; background: rgba(20, 35, 61, 0.07); }

    .cmp-chip { border: 1px solid transparent; }
    .cmp-chip--neutral { background: #f1f6fd; color: var(--rx-text); }
    .cmp-chip--current { background: #eaf2ff; color: var(--rx-blue); }
    .cmp-chip--variant,
    .cmp-chip--accent { background: #e8faf6; color: var(--rx-teal); }

    .cmp-empty,
    .cmp-section-summary,
    .cmp-inline-summary,
    .cmp-item-card__summary,
    .cmp-eval-card__summary,
    .cmp-process-card__summary,
    .cmp-process-card__issue,
    .cmp-provider__hero-text,
    .cmp-change-card__summary {
      margin: 0;
      color: var(--rx-muted);
      line-height: 1.7;
      font-size: 14px;
    }

    .cmp-empty {
      padding: 12px 14px;
      border-radius: var(--rx-radius-sm);
      background: rgba(20, 35, 61, 0.04);
    }

    .cmp-card-list,
    .cmp-eval-grid {
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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
      border: 1px solid rgba(104, 132, 171, 0.16);
      border-radius: var(--rx-radius-md);
      background: rgba(255, 255, 255, 0.92);
    }

    .cmp-fold + .cmp-fold,
    .cmp-process-section + .cmp-process-section { margin-top: 12px; }
    .cmp-fold summary,
    .cmp-process-section summary,
    .cmp-evidence summary { list-style: none; cursor: pointer; }
    .cmp-fold summary::-webkit-details-marker,
    .cmp-process-section summary::-webkit-details-marker,
    .cmp-evidence summary::-webkit-details-marker { display: none; }

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
    .cmp-evidence__body { padding: 0 18px 18px; }

    .cmp-subsection + .cmp-subsection { margin-top: 18px; }
    .cmp-subsection h4,
    .cmp-profile-group__title {
      margin: 0 0 10px;
      font-size: 15px;
      line-height: 1.4;
    }

    .cmp-item-card,
    .cmp-eval-card,
    .cmp-process-card {
      padding: 14px 15px;
      border-radius: var(--rx-radius-sm);
    }

    .cmp-evidence { margin-top: 10px; background: rgba(20, 35, 61, 0.03); box-shadow: none; }
    .cmp-evidence summary { padding: 10px 12px; color: var(--rx-blue); font-size: 13px; font-weight: 700; }
    .cmp-evidence__body p { margin: 0; color: var(--rx-muted); line-height: 1.7; font-size: 13px; }
    .cmp-evidence__body p + p { margin-top: 8px; }

    .cmp-inline-note,
    .cmp-inline-summary { margin-bottom: 10px; }

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
      color: var(--rx-muted);
      font-size: 12px;
      font-style: normal;
    }

    .cmp-process-section summary { padding: 14px 16px; }
    .cmp-process-section__list { display: grid; gap: 10px; }

    .cmp-score-line + .cmp-score-line { margin-top: 10px; }
    .cmp-score-line__row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 7px;
      font-size: 13px;
      color: var(--rx-muted);
    }

    .cmp-score-line__track {
      height: 8px;
      border-radius: 999px;
      background: rgba(20, 35, 61, 0.08);
      overflow: hidden;
    }

    .cmp-score-line__track span {
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(36, 107, 206, 0.86), rgba(15, 143, 127, 0.82));
    }

    .cmp-transcript {
      display: grid;
      gap: 12px;
      max-height: 760px;
      overflow: auto;
      padding-right: 6px;
    }

    .cmp-bubble {
      max-width: 92%;
      padding: 14px 16px;
      border-radius: 18px 18px 18px 8px;
      background: #f4f8ff;
      border: 1px solid rgba(36, 107, 206, 0.14);
    }

    .cmp-bubble--consultant { background: #edf5ff; }
    .cmp-bubble--doctor { background: #fff4e3; border-color: rgba(191, 122, 25, 0.18); }
    .cmp-bubble--customer {
      margin-left: auto;
      border-radius: 18px 18px 8px 18px;
      background: #ecfbf8;
      border-color: rgba(15, 143, 127, 0.16);
    }
    .cmp-bubble--neutral { background: #f5f7fb; }

    .cmp-bubble__head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--rx-muted);
    }

    .cmp-bubble__speaker { font-weight: 700; color: var(--rx-text); }
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
      border-radius: var(--rx-radius-md);
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
      .rx-hero,
      .rx-summary-grid,
      .rx-compare-grid { grid-template-columns: 1fr; }
    }

    @media (max-width: 820px) {
      .rx-page { padding: 16px 14px 28px; }
      .rx-hero, .rx-section, .cmp-provider { padding: 18px; }
      .rx-summary-card__metrics,
      .cmp-provider__metrics,
      .cmp-score-hero { grid-template-columns: 1fr; }
      .cmp-provider__head,
      .rx-section__head,
      .rx-summary-card__head { flex-direction: column; align-items: stretch; }
      .cmp-bubble { max-width: 100%; }
    }
    """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>录音对比报告</title>
  <style>{styles}</style>
</head>
<body>
  <main class="rx-page">
    <header class="rx-hero">
      <section>
        <span class="rx-hero__eyebrow">录音分析结果详情 · 精简对比视图</span>
        <h1 class="rx-hero__title">{file_name}</h1>
        <p class="rx-hero__subtitle">这一版只保留你现在最关心的两套结果：当前腾讯 ASR 和讯飞 `roleType=1`。页面优先展示成交判断、角色分离、关键识别项和模块化分析结果，把原始 diff 收到最后，阅读负担会比之前小很多。</p>
        <div class="rx-pill-row">
          {cmp._render_chip(f"录音ID {recording_id}", tone="neutral")}
          {cmp._render_chip("当前结果 tencent_asr", tone="current")}
          {cmp._render_chip("对比结果 xfyun roleType=1", tone="variant")}
          {cmp._render_chip(f"创建时间 {created_at}", tone="neutral")}
        </div>
      </section>
      <aside class="rx-hero__aside">
        {cmp._render_metric("音频时长", cmp._format_duration(current.duration_ms or variant.duration_ms))}
        {cmp._render_metric("原始音频", "可直接试听", "下面的音频播放器指向同一份原始录音")}
        {cmp._render_metric("当前页面", "只读对比页", "不会覆盖数据库里的原始转写和分析结果")}
      </aside>
    </header>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>音频与产物</h2>
          <p>这里保留原始音频和本次对比用到的 JSON / diff 文件，方便回看或继续排查。</p>
        </div>
        <div class="rx-note">这份页面是从现有对比目录重建出来的，没有重新写入原始 transcript / analysis。</div>
      </div>
      <audio controls preload="metadata" style="width:100%;min-height:58px;border-radius:999px;background:rgba(236,244,255,.92);" src="{audio_href}">您的浏览器暂不支持音频播放。</audio>
      <div class="rx-link-grid" style="margin-top:16px;">{source_links}</div>
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>总览卡片</h2>
          <p>先看整体状态、角色分布和评分，再决定要不要往下看全文和 diff。</p>
        </div>
      </div>
      <div class="rx-summary-grid">{summary_cards_html}</div>
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>关键变化</h2>
          <p>把最值得看的差异直接抬到上面：角色分离、成交判断、适应症、顾虑和推荐方案。</p>
        </div>
      </div>
      <div class="rx-change-grid">{key_changes_html}</div>
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>横向对照表</h2>
          <p>如果你想快速扫一眼两套结果在每个模块上的差异，这里比全文更省力。</p>
        </div>
      </div>
      {matrix_table_html}
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>模块化详情</h2>
          <p>沿用现在录音分析详情页的组织方式，把主诉、适应症、顾虑、推荐、评分和过程评价分别展开。</p>
        </div>
      </div>
      <div class="rx-compare-grid">
        {cmp._render_provider_panel(label=current.label, provider_name=current.provider_name, accent=current.accent, utterances=current.utterances, full_text=current.full_text, duration_ms=current.duration_ms, result=current.analysis)}
        {cmp._render_provider_panel(label=variant.label, provider_name=variant.provider_name, accent=variant.accent, utterances=variant.utterances, full_text=variant.full_text, duration_ms=variant.duration_ms, result=variant.analysis)}
      </div>
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>对话全文</h2>
          <p>默认还是保留全文对照，但放在后面，避免一打开就被大段文本淹没。</p>
        </div>
      </div>
      <div class="rx-compare-grid">
        {cmp._render_transcript_panel(title=current.label, accent=current.accent, utterances=current.utterances, duration_ms=current.duration_ms, full_text=current.full_text)}
        {cmp._render_transcript_panel(title=variant.label, accent=variant.accent, utterances=variant.utterances, duration_ms=variant.duration_ms, full_text=variant.full_text)}
      </div>
    </section>

    <section class="rx-section">
      <div class="rx-section__head">
        <div>
          <h2>原始 Diff</h2>
          <p>需要做工程排查时再看这里；它保留了完整文本差异，但不再占据页面主视角。</p>
        </div>
      </div>
      <div class="rx-compare-grid">
        <article class="cmp-provider cmp-provider--current cmp-provider--transcript">
          <div class="cmp-provider__head"><div><span class="cmp-provider__eyebrow">文本差异</span><h3 class="cmp-provider__title">转写 Diff</h3></div></div>
          <pre>{cmp.html.escape(transcript_diff_text)}</pre>
        </article>
        <article class="cmp-provider cmp-provider--variant cmp-provider--transcript">
          <div class="cmp-provider__head"><div><span class="cmp-provider__eyebrow">结构差异</span><h3 class="cmp-provider__title">分析 Diff</h3></div></div>
          <pre>{cmp.html.escape(analysis_diff_text)}</pre>
        </article>
      </div>
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild a more readable compare page from an existing XFYUN compare directory.")
    parser.add_argument("--compare-dir", required=True, help="Existing compare output directory")
    args = parser.parse_args()

    compare_dir = Path(args.compare_dir).resolve()
    if not compare_dir.is_dir():
        raise SystemExit(f"Compare directory not found: {compare_dir}")

    metadata = _load_json(compare_dir / "metadata.json")
    current = _variant_from_files(
        label="当前腾讯",
        provider_name=str(_load_json(compare_dir / "current_transcript.json").get("provider") or "tencent_asr"),
        accent="current",
        transcript_path=compare_dir / "current_transcript.json",
        analysis_path=compare_dir / "current_analysis.json",
    )
    variant = _variant_from_files(
        label="讯飞 roleType=1",
        provider_name="xfyun_asr · roleType=1",
        accent="variant",
        transcript_path=compare_dir / "xfyun_transcript.json",
        analysis_path=compare_dir / "xfyun_analysis.json",
    )

    settings = get_settings()
    audio_path = settings.resolve_file_path(str(metadata.get("file_path") or ""))
    if not audio_path.is_file():
        raise SystemExit(f"Audio file not found from metadata: {audio_path}")

    html_text = _render_layout(
        compare_dir=compare_dir,
        recording_id=str(metadata.get("recording_id") or "-"),
        file_name=str(metadata.get("file_name") or compare_dir.name),
        created_at=cmp._format_datetime(metadata.get("created_at")),
        audio_href=cmp._relative_href(compare_dir, audio_path),
        current=current,
        variant=variant,
        transcript_diff_text=(compare_dir / "transcript.diff").read_text(encoding="utf-8"),
        analysis_diff_text=(compare_dir / "analysis.diff").read_text(encoding="utf-8"),
    )
    (compare_dir / "compare_report.html").write_text(html_text, encoding="utf-8")

    print(json.dumps({"report_html": str(compare_dir / "compare_report.html")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
