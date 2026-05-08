from smart_badge_api.db.default_data import (
    DEFAULT_QUALITY_DIMENSIONS,
    DEFAULT_RULE_GROUPS,
    DEFAULT_SUMMARY_TEMPLATES,
    DEFAULT_TAG_CATEGORIES,
)
from smart_badge_api.schemas.quality import QualityDimensionCreate
from smart_badge_api.schemas.rule_groups import RuleGroupCreate
from smart_badge_api.schemas.templates import SummaryTemplateCreate


def test_rule_group_defaults_created_by_admin() -> None:
    rule_group = RuleGroupCreate(name="默认规则组")

    assert rule_group.created_by == "admin"
    assert rule_group.detail == ""


def test_summary_template_supports_rule_group_binding() -> None:
    template = SummaryTemplateCreate(
        name="通用总结模板",
        template_type="one_line_summary",
        content="请总结本次咨询的核心结论。",
        rule_group_id="rg001",
    )

    assert template.rule_group_id == "rg001"


def test_quality_dimension_supports_rule_group_binding() -> None:
    dimension = QualityDimensionCreate(
        name="需求探寻",
        description="是否挖掘核心需求",
        rule_group_id="rg002",
        weight=1.5,
    )

    assert dimension.rule_group_id == "rg002"
    assert dimension.weight == 1.5


def test_operation_center_defaults_cover_primary_pages() -> None:
    rule_group_names = {item["name"] for item in DEFAULT_RULE_GROUPS}
    template_types = {item["template_type"] for item in DEFAULT_SUMMARY_TEMPLATES}
    dimension_names = {item["name"] for item in DEFAULT_QUALITY_DIMENSIONS}
    assert "通用接诊匹配规则" in rule_group_names
    assert "高价值客户规则" in rule_group_names
    assert "customer_value" not in template_types
    assert "visit_quality" in template_types
    assert "需求探寻" in dimension_names
    assert "机构优势" in dimension_names
    assert DEFAULT_TAG_CATEGORIES == []


def test_operation_center_defaults_reference_existing_rule_groups() -> None:
    rule_group_names = {item["name"] for item in DEFAULT_RULE_GROUPS}

    for template in DEFAULT_SUMMARY_TEMPLATES:
        assert template["rule_group_name"] in rule_group_names

    for dimension in DEFAULT_QUALITY_DIMENSIONS:
        assert dimension["rule_group_name"] in rule_group_names

    for category in DEFAULT_TAG_CATEGORIES:
        assert category["rule_group_name"] in rule_group_names
