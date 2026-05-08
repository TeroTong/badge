from smart_badge_api.api.routes.tags import _is_hidden_tag_category_name
from smart_badge_api.tag_catalog_reference import (
    canonicalize_profile_tag_category,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    load_tag_catalog_definitions,
)
from pathlib import Path
import re


def test_hidden_tag_category_name_marks_budget_as_non_configurable():
    assert _is_hidden_tag_category_name("本次消费预算") is True


def test_hidden_tag_category_name_keeps_regular_profile_tags_visible():
    assert _is_hidden_tag_category_name("出生日期") is False


def test_label_catalog_reference_uses_new_label_sheet_entries():
    definitions = load_tag_catalog_definitions()
    names = {item.name for item in definitions}
    negative_project = next(item for item in definitions if item.name == "负面项目/设备/原材料")

    assert "常驻城市" in names
    assert "本次消费预算" not in names
    assert "对比机构" not in names
    assert negative_project.description == '仅在已明确存在治疗历史但未提取到负面项目/设备/原材料时，或已明确客户未做过医美治疗时，填"无"；若既往治疗本身未提取到，则留空；提取到则填具体项目/设备/原材料名称'
    assert negative_project.options == ()


def test_canonicalize_profile_tag_category_maps_legacy_names_to_new_catalog():
    assert canonicalize_profile_tag_category("常住城市") == "常驻城市"
    assert canonicalize_profile_tag_category("负面项目/设备/原材料名称") == "负面项目/设备/原材料"
    assert canonicalize_profile_tag_category("出生日期") == "出生日期"
    assert canonicalize_profile_tag_category("年龄") == "出生日期"
    assert canonicalize_profile_tag_category("既往医美治疗") == "治疗历史"
    assert canonicalize_profile_tag_category("喜好治疗方式") == "倾向治疗方式"
    assert canonicalize_profile_tag_category("其他信息") == "其它信息"
    assert canonicalize_profile_tag_category("对比机构") is None


def test_canonicalize_profile_tag_value_maps_common_enum_variants() -> None:
    assert canonicalize_profile_tag_value("价格敏感度", "较高") == "高"
    assert canonicalize_profile_tag_value("亲属/子女情况", "2孩") == "2孩及以上"
    assert canonicalize_profile_tag_value("决策主体", "自主决策") == "自主"
    assert canonicalize_profile_tag_value("常驻城市", "外地（沈阳）") == "外地"
    assert canonicalize_profile_tag_value("治疗项目", "提眉手术") == "手术类"


def test_is_valid_profile_tag_value_rejects_unmapped_enum_noise() -> None:
    assert is_valid_profile_tag_value("常驻城市", "成都") is False
    assert is_valid_profile_tag_value("个人情况", "在校学生") is False
    assert is_valid_profile_tag_value("治疗项目", "未做过医美项目") is True
    assert canonicalize_profile_tag_value("治疗项目", "第一次做这个手术") is None
    assert is_valid_profile_tag_value("治疗项目", "提眉") is False


def test_canonicalize_profile_tag_value_drops_open_text_placeholder_variants() -> None:
    assert canonicalize_profile_tag_value("历史用的设备/原材料名称", "未提及具体设备") is None
    assert canonicalize_profile_tag_value("居住地址", "未说明具体住址") is None


def test_frontend_tag_catalog_matches_label_catalog_reference():
    frontend_path = Path(__file__).resolve().parents[2] / "web" / "src" / "constants" / "tag-catalog.ts"
    text = frontend_path.read_text(encoding="utf-8")
    entries = re.findall(
        r"\{ name: '([^']+)', group: '([^']+)', weight: (\d+), description: '([^']*)', options: \[([^\]]*)\] \}",
        text,
    )

    frontend_items: list[tuple[int, str, str, str, tuple[str, ...]]] = []
    for name, group, weight, description, options_text in entries:
        options = tuple(match.group(1) for match in re.finditer(r"'([^']*)'", options_text))
        frontend_items.append((int(weight), group, name, description, options))

    reference_items = [
        (
            item.weight_level,
            item.group_name,
            item.name,
            item.description,
            item.options,
        )
        for item in load_tag_catalog_definitions()
    ]

    assert frontend_items == reference_items
