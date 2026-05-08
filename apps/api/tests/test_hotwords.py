from __future__ import annotations

import pytest
from fastapi import HTTPException

from smart_badge_api.api.routes.hotwords import _normalize_word
from smart_badge_api.schemas.hotwords import HotwordCreate, HotwordGroupCreate


def test_hotword_group_defaults_include_scope_and_source() -> None:
    group = HotwordGroupCreate(name="竞品机构热词", group_type="行业")

    assert group.library_scope == "public"
    assert group.source_label == "行业"


def test_hotword_create_defaults_weight_to_ten() -> None:
    word = HotwordCreate(word="米兰柏羽")

    assert word.weight == 10
    assert word.is_active is True


def test_normalize_word_trims_and_rejects_empty_values() -> None:
    assert _normalize_word("  新氧  ") == "新氧"

    with pytest.raises(HTTPException, match="热词不能为空"):
        _normalize_word("   ")
