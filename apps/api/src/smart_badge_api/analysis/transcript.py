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
_CUSTOMER_SIDE_RAW_ROLES = {"customer", "client", "patient", "primary_customer", "visitor_companion", "visitor", "客户", "主客户", "同行人", "访客"}
_CUSTOMER_SIDE_BUSINESS_ROLES = {"customer", "client", "patient", "primary_customer", "visitor_companion", "visitor"}
_STAFF_SIDE_BUSINESS_ROLES = {
    "badge_owner",
    "consultant",
    "advisor",
    "staff_peer",
    "doctor",
    "frontdesk",
    "front_desk",
    "reception",
    "expert_assistant",
    "doctor_assistant",
    "assistant",
}
_STAFF_OVERRIDE_ROLE = "consultant"
_STAFF_OVERRIDE_LABEL = "咨询师"

_STAFF_ADDRESS_CUES = (
    "给你",
    "给您",
    "帮你",
    "帮您",
    "建议你",
    "建议您",
    "我会给你",
    "我会给您",
    "我给你",
    "我给您",
    "我帮你",
    "我帮您",
    "我带你",
    "我带您",
    "你可以",
    "您可以",
    "你要",
    "您要",
    "你的",
    "您的",
    "你现在",
    "您现在",
    "对于你",
    "对你来讲",
    "对您来讲",
)
_STAFF_PROFESSIONAL_CUES = (
    "建议",
    "推荐",
    "适合",
    "方案",
    "材料",
    "玻尿酸",
    "瑞德喜",
    "胶原",
    "注射",
    "填充",
    "支撑",
    "馒化",
    "颧骨",
    "颧弓",
    "颧突",
    "鼻基底",
    "中面部",
    "面中",
    "泪沟",
    "眼袋",
    "苹果肌",
    "上眼窝",
    "脂肪",
    "内切",
    "外切",
    "回填",
    "凹陷",
    "术前",
    "模拟",
    "存活率",
    "法令纹",
    "下巴",
    "嘴唇",
    "嘴巴",
    "部位",
    "几支",
    "支数",
    "一支",
    "两支",
    "每边",
    "每个",
)
_STAFF_CONCLUSION_CUES = (
    "我觉得你",
    "我不建议",
    "我建议",
    "我推荐",
    "我会打",
    "我会给",
    "我就会给你",
    "我就会给您",
    "我的建议",
    "我的方案",
    "我一定要跟你说",
    "我要做的底线",
    "刚才的支数",
    "医生建议",
    "老师会",
    "带下一位面诊",
)
_NON_DOCTOR_STAFF_CUES = (
    "专家助理",
    "医生助理",
    "医助",
    "院长助理",
    "咨询助理",
    "王院长的手术",
    "约了我们王院长",
    "我去看一下他的手术",
    "先让他看一下",
    "帮我面诊",
    "喊他面诊",
    "我带顾客来",
)
_STAFF_OPERATION_CUES = (
    "我的顾客",
    "我有个顾客",
    "我那个顾客",
    "那个顾客",
    "这个顾客",
    "接顾客",
    "带顾客",
    "开检查单",
    "开单",
    "做检查",
    "前台",
    "值班医生",
    "医生值班",
    "我同事",
    "给我同事",
    "到账之后",
    "到账后",
    "买单",
    "排在",
    "手术台",
)
_STAFF_SIGNATURE_FLOW_CUES = (
    "护士长",
    "忘记签字",
    "自己写的",
    "他自己写",
    "她自己写",
    "喊",
    "不用盖章",
)
_CURRENT_CUSTOMER_SELF_CUES = (
    "我想",
    "我希望",
    "我主要",
    "我怕",
    "我担心",
    "我以前",
    "我之前",
    "我做过",
    "我打过",
    "我没做过",
    "我没有",
    "我有",
    "多少钱",
    "价格",
    "预算",
)
_DOCTOR_EXPLANATION_ADDRESS_CUES = (
    "给你讲一下",
    "给您讲一下",
    "你看",
    "您看",
    "你这个",
    "您的",
    "你的",
    "你要清楚",
    "我要告诉你",
    "我可以把你",
    "是不是感觉",
)
_DOCTOR_EXPLANATION_FLOW_CUES = (
    "不仅仅是",
    "除了",
    "其实",
    "为什么",
    "正常你",
    "整体",
    "我术前",
    "通过模拟",
    "推平整",
    "分开来",
    "合在一起看",
)


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


def _normalized_role_key(value: object) -> str:
    return _clean_text(value).lower()


def _is_customer_side_role(value: object) -> bool:
    role = _normalized_role_key(value)
    return role in {item.lower() for item in _CUSTOMER_SIDE_RAW_ROLES} or normalize_role(role) in {
        "客户",
        "主客户",
        "同行人",
        "访客",
    }


def _is_staff_side_role(value: object) -> bool:
    role = _normalized_role_key(value)
    return role in _STAFF_SIDE_BUSINESS_ROLES or normalize_role(role) in {
        "咨询师",
        "医生",
        "前台",
        "工牌本人",
        "员工同事",
    }


def _is_customer_side_label(value: object) -> bool:
    label = _clean_text(value)
    if not label:
        return False
    lowered = label.lower()
    return lowered in _CUSTOMER_SIDE_BUSINESS_ROLES or any(term in label for term in ("主客户", "同行人", "客户", "顾客", "访客"))


def _is_staff_side_label(value: object) -> bool:
    label = _clean_text(value)
    if not label:
        return False
    lowered = label.lower()
    return lowered in _STAFF_SIDE_BUSINESS_ROLES or any(
        term in label for term in ("工牌本人", "咨询师", "医生", "顾问", "助理", "护士", "员工", "前台")
    )


def _staff_role_from_business_role(value: object) -> str:
    role = _normalized_role_key(value)
    if role == "badge_owner":
        return _STAFF_OVERRIDE_ROLE
    if role in {"frontdesk", "front_desk", "reception"}:
        return "frontdesk"
    if role in {"expert_assistant", "doctor_assistant", "assistant"}:
        return "expert_assistant"
    if role in {"doctor", "staff_peer", "consultant"}:
        return role
    if role == "advisor":
        return "consultant"
    return _STAFF_OVERRIDE_ROLE


def _sanitize_speaker_role_consistency(segment: dict[str, Any]) -> dict[str, Any]:
    """Keep semantic role and identity label from contradicting each other.

    ASR role inference and badge-owner/staff identity resolution are produced by
    different steps. When they disagree, prefer the explicit business identity
    so downstream prompts do not receive impossible labels such as
    "客户（某某（工牌本人））".
    """
    role = _clean_text(segment.get("role") or segment.get("speaker_role"))
    speaker_role = _clean_text(segment.get("speaker_role") or role)
    business_role = _clean_text(segment.get("speaker_business_role"))
    label = _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label"))

    staff_identity = _is_staff_side_role(business_role) or _is_staff_side_label(label)
    customer_role = _is_customer_side_role(role) or _is_customer_side_role(speaker_role)
    if staff_identity and customer_role:
        corrected = dict(segment)
        corrected.setdefault("role_consistency_corrected_from", role or speaker_role)
        corrected_role = _staff_role_from_business_role(business_role)
        corrected["role"] = corrected_role
        corrected["speaker_role"] = corrected_role
        if not _clean_text(corrected.get("speaker_business_role")):
            corrected["speaker_business_role"] = corrected_role
        return corrected

    staff_role = _is_staff_side_role(role) or _is_staff_side_role(speaker_role)
    customer_label = _is_customer_side_label(label) and not _is_staff_side_label(label)
    if staff_role and customer_label:
        corrected = dict(segment)
        corrected.setdefault("speaker_label_consistency_corrected_from", label)
        corrected.pop("speaker_label", None)
        corrected.pop("speaker_display_label", None)
        return corrected

    return segment


def _looks_like_staff_speech_mislabeled_as_customer(segment: dict[str, Any]) -> bool:
    role = _clean_text(segment.get("role") or segment.get("speaker_role")).lower()
    business_role = _clean_text(segment.get("speaker_business_role")).lower()
    label = _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label")).lower()
    if not ({role, business_role, label} & {item.lower() for item in _CUSTOMER_SIDE_RAW_ROLES}):
        return False
    text = re.sub(r"\s+", "", _clean_text(segment.get("text")))
    if not text:
        return False
    # 明确的一人称顾客诉求、预算或追问不能改成咨询师。
    if any(cue in text for cue in ("我想", "我希望", "我主要", "我怕", "我担心", "我以前", "我之前", "我做过", "我打过", "我没做过", "我没有", "我有", "大概要几支", "多少钱", "价格")):
        if not any(cue in text for cue in _STAFF_CONCLUSION_CUES):
            return False
    if any(cue in text for cue in _STAFF_CONCLUSION_CUES):
        return True
    address_hits = sum(cue in text for cue in _STAFF_ADDRESS_CUES)
    professional_hits = sum(cue in text for cue in _STAFF_PROFESSIONAL_CUES)
    return address_hits >= 1 and professional_hits >= 1


def _looks_like_non_doctor_staff_speech(segment: dict[str, Any]) -> bool:
    text = re.sub(r"\s+", "", _clean_text(segment.get("text")))
    return bool(text and any(cue in text for cue in _NON_DOCTOR_STAFF_CUES))


def _looks_like_staff_operations_mislabeled_as_customer(segment: dict[str, Any]) -> bool:
    role = _clean_text(segment.get("role") or segment.get("speaker_role")).lower()
    business_role = _clean_text(segment.get("speaker_business_role")).lower()
    label = _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label")).lower()
    if not ({role, business_role, label} & {item.lower() for item in _CUSTOMER_SIDE_RAW_ROLES}):
        return False
    text = re.sub(r"\s+", "", _clean_text(segment.get("text")))
    if not text:
        return False
    if any(cue in text for cue in _STAFF_OPERATION_CUES):
        # "我有" alone can be a customer's personal statement, but "我有个顾客"
        # / "我的顾客" / front-desk and order-flow wording are staff operations.
        if "我有个顾客" in text or "我的顾客" in text or "我那个顾客" in text:
            return True
        if any(cue in text for cue in ("接顾客", "带顾客", "前台", "值班医生", "医生值班", "手术台", "给我同事")):
            return True
        if any(cue in text for cue in ("开检查单", "开单", "做检查", "到账之后", "到账后", "买单")) and any(
            address in text for address in ("给你", "给您", "帮你", "帮您", "你", "您")
        ):
            return True
    return False


def _looks_like_staff_signature_flow_mislabeled_as_customer(segment: dict[str, Any]) -> bool:
    role = _clean_text(segment.get("role") or segment.get("speaker_role")).lower()
    business_role = _clean_text(segment.get("speaker_business_role")).lower()
    label = _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label")).lower()
    if not ({role, business_role, label} & {item.lower() for item in _CUSTOMER_SIDE_RAW_ROLES}):
        return False
    text = re.sub(r"\s+", "", _clean_text(segment.get("text")))
    if not text or ("签字" not in text and "盖章" not in text):
        return False
    if "喊" in text and "签字" in text:
        return True
    return any(cue in text for cue in _STAFF_SIGNATURE_FLOW_CUES)


def _looks_like_doctor_explanation_mislabeled_as_customer(segment: dict[str, Any]) -> bool:
    role = _clean_text(segment.get("role") or segment.get("speaker_role")).lower()
    business_role = _clean_text(segment.get("speaker_business_role")).lower()
    label = _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label")).lower()
    if not ({role, business_role, label} & {item.lower() for item in _CUSTOMER_SIDE_RAW_ROLES}):
        return False
    text = re.sub(r"\s+", "", _clean_text(segment.get("text")))
    if len(text) < 18:
        return False
    has_address = any(cue in text for cue in _DOCTOR_EXPLANATION_ADDRESS_CUES)
    if not has_address:
        return False
    professional_hits = sum(cue in text for cue in _STAFF_PROFESSIONAL_CUES)
    flow_hits = sum(cue in text for cue in _DOCTOR_EXPLANATION_FLOW_CUES)
    if professional_hits >= 2 and flow_hits >= 1:
        return True
    return professional_hits >= 3 and len(text) >= 36


def _apply_speaker_role_correction(segment: dict[str, Any]) -> dict[str, Any]:
    segment = _sanitize_speaker_role_consistency(segment)
    if _looks_like_staff_signature_flow_mislabeled_as_customer(segment):
        corrected = dict(segment)
        corrected["role_corrected_from"] = _clean_text(segment.get("role") or segment.get("speaker_role"))
        corrected["speaker_label_corrected_from"] = _clean_text(
            segment.get("speaker_label") or segment.get("speaker_display_label")
        )
        corrected["role"] = "staff_peer"
        corrected["speaker_role"] = "staff_peer"
        corrected["speaker_business_role"] = "staff_peer"
        corrected["speaker_label"] = "员工同事"
        return corrected
    if _looks_like_staff_operations_mislabeled_as_customer(segment):
        corrected = dict(segment)
        corrected["role_corrected_from"] = _clean_text(segment.get("role") or segment.get("speaker_role"))
        corrected["speaker_label_corrected_from"] = _clean_text(
            segment.get("speaker_label") or segment.get("speaker_display_label")
        )
        corrected["role"] = "staff_peer"
        corrected["speaker_role"] = "staff_peer"
        corrected["speaker_business_role"] = "staff_peer"
        corrected["speaker_label"] = "员工同事"
        return corrected
    if _looks_like_non_doctor_staff_speech(segment):
        corrected = dict(segment)
        corrected["role_corrected_from"] = _clean_text(segment.get("role") or segment.get("speaker_role"))
        corrected["speaker_label_corrected_from"] = _clean_text(
            segment.get("speaker_label") or segment.get("speaker_display_label")
        )
        corrected["role"] = _STAFF_OVERRIDE_ROLE
        corrected["speaker_role"] = _STAFF_OVERRIDE_ROLE
        corrected["speaker_business_role"] = _STAFF_OVERRIDE_ROLE
        corrected["speaker_label"] = "专家助理" if "专家助理" in _clean_text(segment.get("text")) else _STAFF_OVERRIDE_LABEL
        return corrected
    if _looks_like_doctor_explanation_mislabeled_as_customer(segment):
        corrected = dict(segment)
        corrected["role_corrected_from"] = _clean_text(segment.get("role") or segment.get("speaker_role"))
        corrected["speaker_label_corrected_from"] = _clean_text(
            segment.get("speaker_label") or segment.get("speaker_display_label")
        )
        corrected["role"] = "doctor"
        corrected["speaker_role"] = "doctor"
        corrected["speaker_business_role"] = "doctor"
        corrected["speaker_label"] = "医生"
        return corrected
    if not _looks_like_staff_speech_mislabeled_as_customer(segment):
        return segment
    corrected = dict(segment)
    corrected["role_corrected_from"] = _clean_text(segment.get("role") or segment.get("speaker_role"))
    corrected["speaker_label_corrected_from"] = _clean_text(
        segment.get("speaker_label") or segment.get("speaker_display_label")
    )
    corrected["role"] = _STAFF_OVERRIDE_ROLE
    corrected["speaker_role"] = _STAFF_OVERRIDE_ROLE
    corrected["speaker_business_role"] = _STAFF_OVERRIDE_ROLE
    corrected["speaker_label"] = _STAFF_OVERRIDE_LABEL
    return corrected


def _is_raw_speaker_label(value: object) -> bool:
    return bool(_RAW_SPEAKER_PATTERN.match(_clean_text(value)))


def _format_speaker_prefix(seg: dict[str, Any]) -> str:
    seg = _sanitize_speaker_role_consistency(seg)
    role = normalize_role(_clean_text(seg.get("role")))
    label = _clean_text(
        seg.get("speaker_label")
        or seg.get("speaker_display_label")
        or seg.get("speaker_name")
    )
    if not label or _is_raw_speaker_label(label):
        return role
    if _is_staff_side_role(seg.get("role")) and _is_customer_side_label(label) and not _is_staff_side_label(label):
        return role
    if _is_customer_side_role(seg.get("role")) and _is_staff_side_label(label):
        return label
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
            _apply_speaker_role_correction({
                "role": role,
                "speaker_label": _clean_text(item.get("speaker_display_label") or item.get("speaker_id")),
                "speaker_role": _clean_text(item.get("speaker_role")) or role,
                "speaker_business_role": _clean_text(item.get("speaker_business_role")) or role,
                "begin": int(item.get("begin_ms", 0) or 0),
                "end": int(item.get("end_ms", 0) or 0),
                "text": text,
            })
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
            copied.setdefault("speaker_role", _clean_text(segment.get("speaker_role")) or _clean_text(segment.get("role")))
            copied.setdefault("speaker_business_role", _clean_text(segment.get("speaker_business_role")) or _clean_text(segment.get("role")))
            if not _clean_text(copied.get("speaker_role")):
                copied["speaker_role"] = _clean_text(segment.get("role"))
            if not _clean_text(copied.get("speaker_business_role")):
                copied["speaker_business_role"] = _clean_text(segment.get("role"))
            copied.setdefault(
                "speaker_label",
                _clean_text(segment.get("speaker_label") or segment.get("speaker_display_label") or segment.get("speaker_id")),
            )
            normalized.append(_apply_speaker_role_correction(copied))
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
