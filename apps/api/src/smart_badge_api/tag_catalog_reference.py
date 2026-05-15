from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


BIRTHDATE_TAG_CATEGORY = "出生日期"
_LEGACY_TAG_CATEGORY_ALIASES = {
    "出生日期": BIRTHDATE_TAG_CATEGORY,
    "出生日期/年龄": BIRTHDATE_TAG_CATEGORY,
    "常住城市": "常驻城市",
    "负面项目/设备/原材料名称": "负面项目/设备/原材料",
    "基本信息_年龄": BIRTHDATE_TAG_CATEGORY,
    "年龄": BIRTHDATE_TAG_CATEGORY,
    "既往医美治疗": "治疗历史",
    "喜好治疗方式": "倾向治疗方式",
    "其他信息": "其它信息",
}
_REMOVED_TAG_CATEGORY_NAMES = frozenset()
NEGATIVE_PROJECT_TAG_CATEGORY = "负面项目/设备/原材料"
NEGATIVE_PROJECT_EMPTY_VALUE = "无"
NEGATIVE_PROJECT_PLACEHOLDER_VALUE = "项目/设备/原材料名称"
NEGATIVE_PROJECT_TAG_DESCRIPTION = '仅在已明确存在治疗历史但未提取到负面项目/设备/原材料时，或已明确客户未做过医美治疗时，填"无"；若既往治疗本身未提取到，则留空；提取到则填具体项目/设备/原材料名称'
_NON_STRICT_OPTION_CATEGORIES = frozenset({"饮品偏好"})
_OPEN_VALUE_PLACEHOLDER_PATTERNS = (
    r"^未提及(?:具体)?",
    r"^未明确(?:具体)?",
    r"^未说明(?:具体)?",
    r"^未知(?:具体)?",
    r"^不详(?:具体)?",
)
_NO_PRIOR_TREATMENT_PATTERNS = (
    r"(?:没|未|没有)(?:有)?做过(?:医美)?(?:项目|治疗|整形)?",
    r"从(?:来)?没做过(?:医美)?(?:项目|治疗|整形)?",
    r"(?:医美|医美项目|项目|治疗|整形).{0,6}(?:就是)?第一次",
    r"第一次做(?:医美|医美项目|项目|治疗|整形)",
    r"无既往(?:医美)?(?:项目|治疗|整形)?",
    r"无医美史",
)
_TREATMENT_PROJECT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("手术类", ("手术类", "外科整形", "提眉", "提眉手术", "眉下切", "切眉", "双眼皮", "双眼皮手术", "重睑", "全切", "埋线双眼皮", "眼袋", "祛眼袋", "去眼袋", "内切眼袋", "外切眼袋", "眶隔脂肪释放", "鼻综合", "隆鼻", "鼻修复", "鼻翼", "鼻头", "山根", "吸脂", "抽脂", "拉皮", "小拉皮", "大拉皮", "隆胸", "丰胸", "线雕", "埋线", "埋线提升")),
    ("注射类", ("注射类", "填充塑形", "除皱瘦脸", "中胚层治疗", "玻尿酸", "玻尿酸填充", "脂肪填充", "自体脂肪", "丰唇", "下巴", "太阳穴", "苹果肌", "肉毒", "除皱针", "瘦脸针", "除皱瘦脸", "水光", "水光针", "中胚层", "中胚层治疗", "童颜", "胶原", "注射", "薇旖美", "贝丽菲尔", "贝利菲尔", "菲利菲尔", "Bellafill")),
    ("光电类", ("光电类", "光电治疗", "光子", "光子嫩肤", "热玛吉", "超声炮", "射频", "黄金微针", "热拉提", "激光")),
)
_TREATMENT_PROJECT_OPTIONS = ("手术类", "注射类", "光电类")
_DRINK_PREFERENCE_OPTIONS = ("咖啡", "茶", "奶茶", "果汁", "气泡水", "白水/矿泉水", "功能饮料", "酒精饮品", "其它")
_LOCAL_CITY_HINTS = (
    "本地",
    "本市",
    "同城",
    "附近",
    "周边",
    "不远",
    "成都",
    "郫都",
    "郫县",
    "双流",
    "新都",
    "成华",
    "锦江",
    "青羊",
    "金牛",
    "武侯",
    "高新",
    "温江",
    "龙泉",
    "天府新区",
    "都江堰",
    "崇州",
    "彭州",
    "新津",
)
_OUT_OF_TOWN_HINTS = (
    "外地",
    "异地",
    "外省",
    "外市",
    "外县",
    "高铁",
    "火车",
    "飞机",
    "单程",
    "小时",
)
_PARTNER_HINTS = ("伴侣", "老公", "丈夫", "爱人", "男友", "女友", "老婆", "妻子")
_PARENT_HINTS = ("父母", "母亲", "妈妈", "父亲", "爸爸")
_CHILD_HINTS = ("儿子", "女儿", "孩子", "小孩", "娃")


@dataclass(frozen=True)
class TagCatalogDefinition:
    name: str
    group_name: str
    weight_level: int
    description: str
    options: tuple[str, ...]
    sort_order: int


_FALLBACK_ROWS: tuple[tuple[int, str, str, str, tuple[str, ...]], ...] = (
    (1, BIRTHDATE_TAG_CATEGORY, BIRTHDATE_TAG_CATEGORY, "", ()),
    (1, "健康风险/禁忌", "健康风险/禁忌", "无风险禁忌、过敏史、疤痕体质、备孕/妊娠/哺乳、精神类疾病、传染性疾病、高血压、糖尿病、心脑血管病、免疫系统疾病", ("无风险禁忌", "过敏史", "疤痕体质", "备孕/妊娠/哺乳", "精神类疾病", "传染性疾病", "高血压", "糖尿病", "心脑血管病", "免疫系统疾病")),
    (1, "治疗历史", "治疗项目", "手术类、注射类、光电类", _TREATMENT_PROJECT_OPTIONS),
    (1, "治疗历史", "历史用的设备/原材料名称", "", ()),
    (1, "治疗历史", NEGATIVE_PROJECT_TAG_CATEGORY, NEGATIVE_PROJECT_TAG_DESCRIPTION, ()),
    (1, "倾向治疗方式", "创伤倾向", "手术、微创、皮肤", ("手术", "微创", "皮肤")),
    (1, "倾向治疗方式", "疼痛耐受度", "高、中、低", ("高", "中", "低")),
    (1, "倾向治疗方式", "效果要求", "即刻、长期", ("即刻", "长期")),
    (1, "倾向治疗方式", "恢复期要求", "1-3天、1周、半个月、1个月以上", ("1-3天", "1周", "半个月", "1个月以上")),
    (1, "倾向治疗方式", "治疗频次", "高频(1月1次)、中频（季度1次）、低频（半年以上1次）", ("高频(1月1次)", "中频（季度1次）", "低频（半年以上1次）")),
    (1, "常驻城市", "常驻城市", "外地、本地", ("外地", "本地")),
    (2, "成交影响因素", "医美目的", "悦己、社交、工作、情感", ("悦己", "社交", "工作", "情感")),
    (2, "成交影响因素", "决策主体", "自主、伴侣、父母、儿女、其它", ("自主", "伴侣", "父母", "儿女", "其它")),
    (2, "成交影响因素", "价格敏感度", "高、中、低", ("高", "中", "低")),
    (2, "成交影响因素", "本次消费预算", "", ()),
    (2, "成交影响因素", "对比机构", "", ()),
    (3, "家庭情况", "个人情况", "单身、有恋人、已婚", ("单身", "有恋人", "已婚")),
    (3, "家庭情况", "亲属/子女情况", "无孩、1孩、2孩及以上", ("无孩", "1孩", "2孩及以上")),
    (3, "居住地址", "居住地址", "", ()),
    (3, "职业", "行业", "房地产/建筑/家居、服饰/美妆、广告/影视/会展、环保/化工/电力、计算机/互联网/通信/电子、教育/培训/法律、金融/保险、酒店/旅游、贸易、美容/保健、其他、汽车/机械、物流/运输/航天、医疗/制药、政府/公共事业、其它", ("房地产/建筑/家居", "服饰/美妆", "广告/影视/会展", "环保/化工/电力", "计算机/互联网/通信/电子", "教育/培训/法律", "金融/保险", "酒店/旅游", "贸易", "美容/保健", "其他", "汽车/机械", "物流/运输/航天", "医疗/制药", "政府/公共事业", "其它")),
    (3, "职业", "职位", "员工、高管、老板、个体工商户、无业", ("员工", "高管", "老板", "个体工商户", "无业")),
    (4, "其它信息", "教育程度", "研究生、本科、其它", ("研究生", "本科", "其它")),
    (4, "其它信息", "交通工具", "打车、骑车、开车", ("打车", "骑车", "开车")),
    (4, "其它信息", "业余爱好", "", ()),
    (4, "其它信息", "饮品偏好", "咖啡、茶、奶茶、果汁、气泡水、白水/矿泉水、功能饮料、酒精饮品、其它", _DRINK_PREFERENCE_OPTIONS),
    (4, "其它信息", "餐食偏好", "清淡、香辣、素食、清真、西餐、其它", ("清淡", "香辣", "素食", "清真", "西餐", "其它")),
    (4, "其它信息", "倾向回访方式", "电话、微信、短信", ("电话", "微信", "短信")),
    (4, "其它信息", "护肤习惯", "", ()),
    (3, "特殊身份", "特殊身份", "黑名单、竞对同行", ("黑名单", "竞对同行")),
)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalized_match_text(value: object) -> str:
    text = str(value or "").strip()
    return re.sub(r"[\s（）()、,，;；/·]+", "", text)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _is_explicit_no_prior_treatment_text(value: object) -> bool:
    text = _normalized_match_text(value)
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _NO_PRIOR_TREATMENT_PATTERNS)


def _is_open_value_placeholder_text(value: object) -> bool:
    text = _normalized_match_text(value)
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _OPEN_VALUE_PLACEHOLDER_PATTERNS)


def _parse_weight(value: object, default: int | None) -> int | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = _clean_text(value)
    if text is None:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _split_options(rule_text: str | None) -> tuple[str, ...]:
    if not rule_text:
        return ()
    parts = [part.strip() for part in re.split(r"[、,，]\s*", rule_text) if part and part.strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return tuple(deduped)


def _normalize_birthdate_text(value: str) -> str | None:
    text = value.strip()
    patterns = (
        (r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})", "full"),
        (r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?", "full"),
        (r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})", "month"),
        (r"(?P<year>\d{4})年(?P<month>\d{1,2})月", "month"),
        (r"(?P<year>\d{4})年", "year"),
    )
    for pattern, mode in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year = int(match.group("year"))
        if mode == "year":
            return f"{year:04d}"
        month = int(match.group("month"))
        if mode == "month":
            return f"{year:04d}-{month:02d}"
        day = int(match.group("day"))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _fallback_definitions() -> tuple[TagCatalogDefinition, ...]:
    definitions = [
        TagCatalogDefinition(
            name=name,
            group_name=group_name,
            weight_level=weight_level,
            description=description,
            options=options,
            sort_order=sort_order,
        )
        for sort_order, (weight_level, group_name, name, description, options) in enumerate(_FALLBACK_ROWS)
    ]
    return _sort_definitions(definitions)


def _sort_definitions(definitions: list[TagCatalogDefinition]) -> tuple[TagCatalogDefinition, ...]:
    ordered = sorted(
        definitions,
        key=lambda item: (item.weight_level or 99, item.sort_order),
    )
    return tuple(
        TagCatalogDefinition(
            name=item.name,
            group_name=item.group_name,
            weight_level=item.weight_level,
            description=item.description,
            options=item.options,
            sort_order=index,
        )
        for index, item in enumerate(ordered)
    )


def _normalize_tag_definition(name: str, rule_text: str) -> tuple[str, tuple[str, ...]]:
    if name == NEGATIVE_PROJECT_TAG_CATEGORY:
        return NEGATIVE_PROJECT_TAG_DESCRIPTION, ()
    if name == "治疗项目":
        return "手术类、注射类、光电类", _TREATMENT_PROJECT_OPTIONS
    if name == "饮品偏好":
        return "咖啡、茶、奶茶、果汁、气泡水、白水/矿泉水、功能饮料、酒精饮品、其它", _DRINK_PREFERENCE_OPTIONS
    return rule_text, _split_options(rule_text)


def _normalize_catalog_label(value: str) -> str:
    return _LEGACY_TAG_CATEGORY_ALIASES.get(value, value)


@lru_cache(maxsize=1)
def load_tag_catalog_definitions() -> tuple[TagCatalogDefinition, ...]:
    # The analysis prompt must be deterministic and must not depend on local
    # Excel files, which may be outdated or deleted. Keep the canonical tag
    # catalog in code and sync it into DB via ensure_tag_categories().
    return _fallback_definitions()


@lru_cache(maxsize=1)
def active_tag_category_names() -> frozenset[str]:
    return frozenset(item.name for item in load_tag_catalog_definitions())


@lru_cache(maxsize=1)
def active_tag_group_names() -> frozenset[str]:
    return frozenset(item.group_name for item in load_tag_catalog_definitions())


@lru_cache(maxsize=1)
def tag_category_options_map() -> dict[str, tuple[str, ...]]:
    return {
        item.name: item.options
        for item in load_tag_catalog_definitions()
        if item.options
    }


def legacy_tag_category_aliases() -> dict[str, str]:
    return dict(_LEGACY_TAG_CATEGORY_ALIASES)


def removed_tag_category_names() -> frozenset[str]:
    return _REMOVED_TAG_CATEGORY_NAMES


def canonicalize_profile_tag_category(name: object) -> str | None:
    text = _clean_text(name)
    if text is None:
        return None

    category_names = active_tag_category_names()
    group_names = active_tag_group_names()
    aliases = legacy_tag_category_aliases()

    def _canonicalize(candidate: str) -> str | None:
        normalized = aliases.get(candidate, candidate)
        if normalized in _REMOVED_TAG_CATEGORY_NAMES:
            return None
        if normalized in category_names or normalized in group_names:
            return normalized
        return None

    direct = _canonicalize(text)
    if direct is not None:
        return direct

    if "_" in text:
        suffix = text.rsplit("_", 1)[1].strip()
        return _canonicalize(suffix)

    return None


def _match_option_by_containment(value: str, options: tuple[str, ...]) -> str | None:
    normalized_value = _normalized_match_text(value)
    matches: list[str] = []
    for option in options:
        normalized_option = _normalized_match_text(option)
        if not normalized_option:
            continue
        if normalized_option in normalized_value or normalized_value in normalized_option:
            if option not in matches:
                matches.append(option)
    return matches[0] if len(matches) == 1 else None


def canonicalize_profile_tag_value(category: object, value: object) -> str | None:
    canonical_category = canonicalize_profile_tag_category(category)
    text = _clean_text(value)
    if canonical_category is None or text is None:
        return None

    if canonical_category == BIRTHDATE_TAG_CATEGORY:
        return _normalize_birthdate_text(text)

    if canonical_category == "治疗项目":
        if _is_open_value_placeholder_text(text):
            return None
        if _is_explicit_no_prior_treatment_text(text):
            return text
        for canonical_value, aliases in _TREATMENT_PROJECT_ALIASES:
            if _contains_any(text, aliases):
                return canonical_value
        return None

    if canonical_category != "治疗项目" and _is_open_value_placeholder_text(text):
        return None

    if canonical_category == "历史用的设备/原材料名称" and _contains_any(text, ("贝丽菲尔", "贝利菲尔", "菲利菲尔", "Bellafill")):
        return "贝丽菲尔"

    options = tag_category_options_map().get(canonical_category, ())
    if not options or canonical_category in _NON_STRICT_OPTION_CATEGORIES:
        return text
    if text in options:
        return text

    if canonical_category == "常驻城市":
        if _contains_any(text, _OUT_OF_TOWN_HINTS):
            return "外地"
        if _contains_any(text, _LOCAL_CITY_HINTS):
            return "本地"

    if canonical_category == "决策主体":
        if "自主" in text:
            return "自主"
        if _contains_any(text, _PARENT_HINTS):
            return "父母"
        if _contains_any(text, _PARTNER_HINTS):
            return "伴侣"
        if _contains_any(text, _CHILD_HINTS):
            return "儿女"

    if canonical_category == "个人情况":
        if "已婚" in text or "结婚" in text:
            return "已婚"
        if "单身" in text:
            return "单身"
        if any(keyword in text for keyword in ("恋人", "男友", "女友")):
            return "有恋人"

    if canonical_category == "亲属/子女情况":
        compact = _normalized_match_text(text)
        if any(keyword in compact for keyword in ("无孩", "无孩子", "没孩子", "未育", "无子女")):
            return "无孩"
        if re.search(r"(?:^|[^0-9])[1一]孩", compact) or "一个孩子" in compact or "一娃" in compact:
            return "1孩"
        if re.search(r"(?:[2二两3-9三四五六七八九].*(?:孩|孩子|娃))", compact):
            return "2孩及以上"

    if canonical_category == "教育程度":
        if any(keyword in text for keyword in ("博士", "硕士", "研究生")):
            return "研究生"
        if "本科" in text:
            return "本科"
        if any(keyword in text for keyword in ("专科", "大专", "高中", "中专", "其它", "其他")):
            return "其它"

    if canonical_category == "交通工具":
        if any(keyword in text for keyword in ("开车", "自驾")):
            return "开车"
        if any(keyword in text for keyword in ("打车", "滴滴", "出租")):
            return "打车"
        if any(keyword in text for keyword in ("骑车", "电动车", "摩托", "自行车")):
            return "骑车"

    if canonical_category == "治疗频次":
        compact = _normalized_match_text(text)
        if any(keyword in compact for keyword in ("每月", "一个月一次", "1月1次", "一月一次")):
            return "高频(1月1次)"
        if any(keyword in compact for keyword in ("季度", "三个月", "3个月", "季度1次")):
            return "中频（季度1次）"
        if any(keyword in compact for keyword in ("半年", "一年", "低频")):
            return "低频（半年以上1次）"

    if canonical_category == "健康风险/禁忌":
        if any(keyword in text for keyword in ("无风险", "无明确风险")):
            return "无风险禁忌"

    matched_option = _match_option_by_containment(text, options)
    if matched_option is not None:
        return matched_option

    if canonical_category == "行业" and any(keyword in text for keyword in ("小生意", "个体", "自己做生意")):
        return "其它"

    return text


def is_valid_profile_tag_value(category: object, value: object) -> bool:
    canonical_category = canonicalize_profile_tag_category(category)
    text = _clean_text(value)
    if canonical_category is None or text is None:
        return False

    options = tag_category_options_map().get(canonical_category, ())
    if canonical_category == "治疗项目" and _is_open_value_placeholder_text(text):
        return False
    if not options or canonical_category in _NON_STRICT_OPTION_CATEGORIES:
        return True
    if canonical_category == "治疗项目" and _is_explicit_no_prior_treatment_text(text):
        return True
    return text in options
