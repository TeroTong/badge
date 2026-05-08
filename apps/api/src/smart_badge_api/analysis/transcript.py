"""录音转写文本加载与预处理。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 角色归一化映射：将 ASR 输出的多种角色名统一为标准角色
_ROLE_MAP: dict[str, str] = {
    "销售": "咨询师",
    "美容顾问": "咨询师",
    "美学顾问": "咨询师",
    "美学设计师": "咨询师",
    "客服": "前台",
    "frontdesk": "前台",
    "reception": "前台",
    "consultant": "咨询师",
    "advisor": "咨询师",
    "医生": "医生",
    "doctor": "医生",
    "客户": "客户",
    "patient": "客户",
    "customer": "客户",
    "client": "客户",
    "badge_owner": "工牌本人",
    "工牌本人": "工牌本人",
    "staff_peer": "员工同事",
    "员工同事": "员工同事",
    "primary_customer": "主客户",
    "主客户": "主客户",
    "visitor_companion": "同行人",
    "同行人": "同行人",
    "visitor": "同行人",
    "访客": "访客",
    "unknown": "其他在场人员",
}
_RAW_SPEAKER_PATTERN = re.compile(r"^speaker[_-]?\d+$", re.IGNORECASE)


def _ms_to_mmss(ms: int) -> str:
    """毫秒 → MM:SS 格式。"""
    total_sec = ms // 1000
    return f"{total_sec // 60:02d}:{total_sec % 60:02d}"


def load_transcript(path: str | Path) -> dict[str, Any]:
    """加载原始 JSON 文件，返回完整字典。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize_role(role: str) -> str:
    """将 ASR 角色名映射为标准角色名。"""
    text = str(role or "").strip()
    if not text:
        return "其他在场人员"
    return _ROLE_MAP.get(text, _ROLE_MAP.get(text.lower(), text))


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_raw_speaker_label(value: object) -> bool:
    return bool(_RAW_SPEAKER_PATTERN.match(_clean_text(value)))


def _format_speaker_prefix(seg: dict[str, Any]) -> str:
    role = normalize_role(_clean_text(seg.get("role")))
    label = _clean_text(
        seg.get("speaker_label")
        or seg.get("speaker_display_label")
        or seg.get("speaker_name")
    )
    if not label or _is_raw_speaker_label(label):
        return role
    normalized_label = normalize_role(label)
    if label == role or normalized_label == role or role in label:
        return label
    return f"{role}（{label}）"


def format_dialogue(segments: list[dict[str, Any]]) -> str:
    """将 transcribeResult 片段列表格式化为带时间戳的对话文本。

    输出格式:
        [00:00-00:14] 咨询师: 你下唇的一个饱满度是有的...
        [00:28-00:31] 客户: 资金没有在我这，在我妈那妈妈。
    """
    lines: list[str] = []
    for seg in segments:
        role = _format_speaker_prefix(seg)
        begin = _ms_to_mmss(seg.get("begin", 0))
        end = _ms_to_mmss(seg.get("end", 0))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"[{begin}-{end}] {role}: {text}")
    return "\n".join(lines)


def _segments_from_archive_utterances(raw: dict[str, Any]) -> list[dict[str, Any]]:
    utterances = raw.get("utterances", [])
    if not isinstance(utterances, list):
        return []
    segments: list[dict[str, Any]] = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        role = (
            _clean_text(item.get("speaker_role"))
            or _clean_text(item.get("speaker"))
            or _clean_text(item.get("speaker_business_role"))
        )
        segments.append(
            {
                "role": role,
                "speaker_label": _clean_text(item.get("speaker_display_label") or item.get("speaker_id")),
                "speaker_role": _clean_text(item.get("speaker_role")),
                "speaker_business_role": _clean_text(item.get("speaker_business_role")),
                "begin": int(item.get("begin_ms", 0) or 0),
                "end": int(item.get("end_ms", 0) or 0),
                "text": text,
            }
        )
    return segments


def extract_transcript_segments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
    segments = payload.get("transcribeResult", []) if isinstance(payload, dict) else []
    if isinstance(segments, list) and segments:
        normalized: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            copied = dict(segment)
            copied.setdefault("speaker_role", _clean_text(segment.get("speaker_role")))
            copied.setdefault("speaker_business_role", _clean_text(segment.get("speaker_business_role")))
            copied.setdefault(
                "speaker_label",
                _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label") or segment.get("speaker_id")),
            )
            normalized.append(copied)
        return normalized
    return _segments_from_archive_utterances(raw)


def prepare_transcript(path: str | Path) -> tuple[str, dict[str, Any]]:
    """加载并预处理转写文件。

    Returns:
        (formatted_dialogue, raw_data)
    """
    raw = load_transcript(path)
    segments = extract_transcript_segments(raw)
    dialogue = format_dialogue(segments)
    return dialogue, raw
