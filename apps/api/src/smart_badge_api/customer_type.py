from __future__ import annotations

from typing import Any


CUSTOMER_TYPE_LABELS = {
    "Q": "新客",
    "V": "老客",
}


def normalize_customer_type_code(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in CUSTOMER_TYPE_LABELS else None


def normalize_customer_type_label(code: Any, text: Any = None) -> str | None:
    normalized_code = normalize_customer_type_code(code)
    if normalized_code:
        return CUSTOMER_TYPE_LABELS[normalized_code]

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return None
    if "老客" in normalized_text or "会员" in normalized_text:
        return "老客"
    if "新客" in normalized_text or "潜客" in normalized_text:
        return "新客"
    return normalized_text


def customer_type_from_visit_order(order: Any) -> tuple[str | None, str | None]:
    code = normalize_customer_type_code(getattr(order, "kut30_dq", None)) or normalize_customer_type_code(
        getattr(order, "khlx_t30", None)
    )
    label = normalize_customer_type_label(
        code,
        getattr(order, "kut30_dq_txt", None) or getattr(order, "khlx_t30", None),
    )
    return code, label
