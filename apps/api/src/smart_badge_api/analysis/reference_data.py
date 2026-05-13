from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Prompt/reference construction is code-owned and must not read repository
# Excel files at runtime.
_STATIC_FEATURE_OBJECTIVES = """\
- 提取客户主诉、标准适应症、画像标签、预算/报价、顾虑、推荐方案、种草方案、成交结果和 SAP 总结素材。
- 所有结论必须有带时间戳原话证据；第三方案例、咨询师泛化介绍、假设话术、角色误标内容不得直接当成客户事实。
- 成交只认客户接受并出现付款/定金/下单/锁档/确定日期/安排治疗等落地动作；未成交需提取具体原因。"""

_STATIC_INDICATION_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("Y1", "外科", "SYZ1002", "双眼皮", "BW1001", "眼部"),
    ("Y1", "外科", "SYZ1003", "眼修复", "BW1001", "眼部"),
    ("Y1", "外科", "SYZ1004", "眼袋", "BW1001", "眼部"),
    ("Y1", "外科", "SYZ1005", "提眉", "BW1001", "眼部"),
    ("Y1", "外科", "SYZ1006", "鼻综合", "BW1002", "鼻部"),
    ("Y1", "外科", "SYZ1007", "鼻翼整形", "BW1002", "鼻部"),
    ("Y1", "外科", "SYZ1008", "鼻修复", "BW1002", "鼻部"),
    ("Y1", "外科", "SYZ1009", "隆胸", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1010", "乳头整形", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1011", "乳晕整形", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1012", "副乳整形", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1013", "胸修复", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1014", "乳房下垂", "BW1003", "胸部"),
    ("Y1", "外科", "SYZ1015", "身体吸脂", "BW1004", "身体"),
    ("Y1", "外科", "SYZ1016", "身体填充", "BW1004", "身体"),
    ("Y1", "外科", "SYZ1017", "面部除皱", "BW1005", "面部"),
    ("Y1", "外科", "SYZ1018", "面部吸脂", "BW1005", "面部"),
    ("Y1", "外科", "SYZ1019", "面部填充", "BW1005", "面部"),
    ("Y1", "外科", "SYZ1020", "假体下巴", "BW1005", "面部"),
    ("Y1", "外科", "SYZ1021", "痣/疤痕/肿瘤切除/唇部塑形/眉部塑形/私密整形/腋臭/胎记", "BW1006", "外科其他"),
    ("Y1", "外科", "SYZ1022", "耳畸形矫正", "BW1007", "耳部"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2001", "颅区（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2002", "额区（H）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2003", "颞区（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2004", "耳部（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2005", "内颊（小O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2006", "外颊（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2007", "眼部（D）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2008", "唇部（D）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2012", "身体"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2013", "私密"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2014", "眉弓线（H）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2015", "鼻额衔接线（H）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2016", "鼻中轴线（H）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2017", "额颅交界线（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2018", "下颌轮廓线（大O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2019", "眶外C线（小O）"),
    ("Y2", "微创", "SYZ2001", "塑美", "BW2020", "babyface线（小O）"),
    ("Y2", "微创", "SYZ2002", "紧致淡纹", "BW2009", "面部"),
    ("Y2", "微创", "SYZ2002", "紧致淡纹", "BW2010", "眼部"),
    ("Y2", "微创", "SYZ2002", "紧致淡纹", "BW2011", "颈部"),
    ("Y2", "微创", "SYZ2002", "紧致淡纹", "BW2012", "身体"),
    ("Y2", "微创", "SYZ2002", "紧致淡纹", "BW2013", "私密"),
    ("Y3", "皮肤", "SYZ3001", "松弛下垂", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3001", "松弛下垂", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3001", "松弛下垂", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3001", "松弛下垂", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3001", "松弛下垂", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3002", "纹路", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3002", "纹路", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3002", "纹路", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3002", "纹路", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3003", "色斑", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3003", "色斑", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3003", "色斑", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3003", "色斑", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3004", "敏感", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3004", "敏感", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3004", "敏感", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3005", "痤疮", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3005", "痤疮", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3005", "痤疮", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3006", "干燥", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3006", "干燥", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3006", "干燥", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3006", "干燥", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3006", "干燥", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3007", "暗黄", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3007", "暗黄", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3007", "暗黄", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3007", "暗黄", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3008", "油脂旺盛", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3008", "油脂旺盛", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3008", "油脂旺盛", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3009", "毛孔", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3009", "毛孔", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3010", "脱毛", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3010", "脱毛", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3010", "脱毛", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3011", "洗纹身/洗眉", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3011", "洗纹身/洗眉", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3011", "洗纹身/洗眉", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3011", "洗纹身/洗眉", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3012", "祛痣/祛疣", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3012", "祛痣/祛疣", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3012", "祛痣/祛疣", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3012", "祛痣/祛疣", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3013", "私密", "BW3005", "私密"),
    ("Y3", "皮肤", "SYZ3014", "红血丝", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3014", "红血丝", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3015", "黑眼圈", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3016", "毛周角化", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3016", "毛周角化", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3017", "纹绣", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3017", "纹绣", "BW3002", "眼部"),
    ("Y3", "皮肤", "SYZ3017", "纹绣", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3017", "纹绣", "BW3005", "私密"),
    ("Y3", "皮肤", "SYZ3018", "脱发", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3019", "毛发种植", "BW3006", "头皮"),
    ("Y3", "皮肤", "SYZ3020", "局部减脂", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3020", "局部减脂", "BW3004", "身体"),
    ("Y3", "皮肤", "SYZ3021", "疤痕", "BW3001", "面部"),
    ("Y3", "皮肤", "SYZ3021", "疤痕", "BW3003", "颈部"),
    ("Y3", "皮肤", "SYZ3021", "疤痕", "BW3004", "身体"),
    ("Y4", "口腔等", "SYZ4001", "正畸", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4002", "种植", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4003", "牙修复", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4004", "牙美白", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4005", "拔牙", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4006", "牙预防", "BW4001", "口腔"),
    ("Y4", "口腔等", "SYZ4007", "生活美容", "BW4002", "其他"),
    ("Y5", "中医", "SYS5001", "身体调理", "BW5001", "身体"),
)

_STATIC_INDICATION_NOTES: dict[tuple[str, str, str], str] = {
    ('Y1', 'SYZ1016', 'BW1004'): '仅脂肪填充',
    ('Y1', 'SYZ1019', 'BW1005'): '仅脂肪填充',
    ('Y2', 'SYZ2001', 'BW2001'): '除脂肪填充以外的材料注射，部位包括：头皮、颅顶、侧颅、后颅、枕骨',
    ('Y2', 'SYZ2001', 'BW2002'): '除脂肪填充以外的材料注射，部位包括：额头',
    ('Y2', 'SYZ2001', 'BW2003'): '除脂肪填充以外的材料注射，部位包括：太阳穴、眼尾外侧、眼眶外侧',
    ('Y2', 'SYZ2001', 'BW2004'): '除脂肪填充以外的材料注射，部位包括：耳基底、耳垂、耳轮等',
    ('Y2', 'SYZ2001', 'BW2005'): '除脂肪填充以外的材料注射，部位包括：中面部、苹果肌、面中部、中面部容量、鼻翼旁、面颊、鼻唇沟、法令纹',
    ('Y2', 'SYZ2001', 'BW2006'): '除脂肪填充以外的材料注射，部位包括：面颊外侧、颊脂垫、颧弓下方、耳朵前方、印第安纹',
    ('Y2', 'SYZ2001', 'BW2007'): '除脂肪填充以外的材料注射，部位包括：眼袋、泪沟、黑眼圈、上睑松弛、上睑凹陷、卧蚕',
    ('Y2', 'SYZ2001', 'BW2008'): '除脂肪填充以外的材料注射，部位包括：嘴唇、嘴巴、唇珠、丘比特弓、口周、薄唇、唇峰、唇纹、人中、木偶纹',
    ('Y2', 'SYZ2001', 'BW2012'): '除脂肪填充以外的材料注射',
    ('Y2', 'SYZ2001', 'BW2013'): '除脂肪填充以外的材料注射，部位包括：会阴、阴道、外阴、阴阜、阴蒂、小阴唇、大阴唇、会阴体、阴道口',
    ('Y2', 'SYZ2001', 'BW2014'): '除脂肪填充以外的材料注射，部位包括：眉弓、眉骨、眉脊、眉锋、眉心、眉头',
    ('Y2', 'SYZ2001', 'BW2015'): '除脂肪填充以外的材料注射，部位包括：双C线、鼻额角、三角区、鼻根',
    ('Y2', 'SYZ2001', 'BW2016'): '除脂肪填充以外的材料注射，部位包括：鼻背、鼻尖、鼻小柱、鼻翼、鼻基底、鼻小柱基底',
    ('Y2', 'SYZ2001', 'BW2017'): '除脂肪填充以外的材料注射，部位包括：发际线、高颅顶',
    ('Y2', 'SYZ2001', 'BW2018'): '除脂肪填充以外的材料注射，部位包括：下颌缘、双下巴、下颌线',
    ('Y2', 'SYZ2001', 'BW2019'): '除脂肪填充以外的材料注射，部位包括：眼尾、颧骨突出、眼外侧',
    ('Y2', 'SYZ2001', 'BW2020'): '除脂肪填充以外的材料注射，部位包括：中面颊区、口角下区',
    ('Y2', 'SYZ2002', 'BW2009'): '除脂肪填充以外的材料注射',
    ('Y2', 'SYZ2002', 'BW2010'): '除脂肪填充以外的材料注射',
    ('Y2', 'SYZ2002', 'BW2011'): '除脂肪填充以外的材料注射',
    ('Y2', 'SYZ2002', 'BW2012'): '除脂肪填充以外的材料注射',
    ('Y2', 'SYZ2002', 'BW2013'): '除脂肪填充以外的材料注射',
    ('Y3', 'SYZ3001', 'BW3001'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3001', 'BW3002'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3001', 'BW3003'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3001', 'BW3004'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3001', 'BW3006'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3002', 'BW3001'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3002', 'BW3002'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3002', 'BW3003'): '提及仪器、中胚层治疗、水光治疗等',
    ('Y3', 'SYZ3002', 'BW3004'): '提及仪器、中胚层治疗、水光治疗等',
}


@dataclass(frozen=True)
class IndicationReferenceItem:
    department_code: str
    department_name: str
    indication_code: str
    indication_name: str
    body_part_code: str
    body_part_name: str
    indication_note: str = ""


@dataclass(frozen=True)
class AnalysisReferenceData:
    feature_objectives: str
    indication_reference: str
    indication_guidance_reference: str
    indication_prompt_reference: str
    indication_catalog_by_code_triplet: dict[tuple[str, str, str], IndicationReferenceItem]
    indication_catalog_by_name_triplet: dict[tuple[str, str, str], IndicationReferenceItem]
    indication_catalog_by_code_pair: dict[tuple[str, str], IndicationReferenceItem]
    indication_catalog_by_name_pair: dict[tuple[str, str], IndicationReferenceItem]
    indication_catalog_by_code_pair_candidates: dict[tuple[str, str], tuple[IndicationReferenceItem, ...]]
    indication_catalog_by_name_pair_candidates: dict[tuple[str, str], tuple[IndicationReferenceItem, ...]]


def _normalize_lookup_token(value: object) -> str:
    return str(value or "").strip()


def _load_static_analysis_reference_data() -> AnalysisReferenceData:
    lines: list[str] = []
    seen_keys: set[tuple[str, str, str]] = set()
    catalog_by_code_triplet: dict[tuple[str, str, str], IndicationReferenceItem] = {}
    catalog_by_name_triplet: dict[tuple[str, str, str], IndicationReferenceItem] = {}
    catalog_by_code_pair: dict[tuple[str, str], IndicationReferenceItem] = {}
    catalog_by_name_pair: dict[tuple[str, str], IndicationReferenceItem] = {}
    code_pair_candidates: dict[tuple[str, str], list[IndicationReferenceItem]] = {}
    name_pair_candidates: dict[tuple[str, str], list[IndicationReferenceItem]] = {}
    guidance_lines: list[str] = []

    for department_code, department_name, indication_code, indication_name, body_part_code, body_part_name in _STATIC_INDICATION_ROWS:
        item = IndicationReferenceItem(
            department_code=department_code,
            department_name=department_name,
            indication_code=indication_code,
            indication_name=indication_name,
            body_part_code=body_part_code,
            body_part_name=body_part_name,
            indication_note=_STATIC_INDICATION_NOTES.get((department_code, indication_code, body_part_code), ""),
        )
        code_triplet = (item.department_code, item.indication_code, item.body_part_code)
        name_triplet = (item.department_name, item.indication_name, item.body_part_name)
        code_pair = (item.indication_code, item.body_part_code)
        name_pair = (item.indication_name, item.body_part_name)

        catalog_by_code_triplet[code_triplet] = item
        catalog_by_name_triplet[name_triplet] = item
        catalog_by_code_pair.setdefault(code_pair, item)
        catalog_by_name_pair.setdefault(name_pair, item)
        code_pair_candidates.setdefault(code_pair, []).append(item)
        name_pair_candidates.setdefault(name_pair, []).append(item)

        dedupe_key = (item.department_code, item.indication_code, item.body_part_code)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        line = (
            f"- {item.department_code}|{item.department_name}|"
            f"{item.indication_code}|{item.indication_name}|"
            f"{item.body_part_code}|{item.body_part_name}"
        )
        lines.append(line)
        if item.indication_note:
            guidance_lines.append(f"{line}；选择说明：{item.indication_note}")

    guidance_reference = "\n".join(guidance_lines)
    prompt_reference = "\n".join(lines)
    if guidance_reference:
        prompt_reference = (
            f"{prompt_reference}\n\n"
            "【适应症选择说明】以下说明只用于帮助判断主诉/方案应对应哪条适应症，"
            "不属于 SAP 编码字段，输出 standardized_indications 时仍只填写上方 6 个字段。\n"
            f"{guidance_reference}"
        )

    return AnalysisReferenceData(
        feature_objectives=_STATIC_FEATURE_OBJECTIVES,
        indication_reference="\n".join(lines),
        indication_guidance_reference=guidance_reference,
        indication_prompt_reference=prompt_reference,
        indication_catalog_by_code_triplet=catalog_by_code_triplet,
        indication_catalog_by_name_triplet=catalog_by_name_triplet,
        indication_catalog_by_code_pair=catalog_by_code_pair,
        indication_catalog_by_name_pair=catalog_by_name_pair,
        indication_catalog_by_code_pair_candidates={key: tuple(value) for key, value in code_pair_candidates.items()},
        indication_catalog_by_name_pair_candidates={key: tuple(value) for key, value in name_pair_candidates.items()},
    )

def resolve_indication_reference_item(
    *,
    department_code: str | None = None,
    department_name: str | None = None,
    indication_code: str | None = None,
    indication_name: str | None = None,
    body_part_code: str | None = None,
    body_part_name: str | None = None,
) -> IndicationReferenceItem | None:
    reference_data = load_analysis_reference_data()
    normalized_department_code = _normalize_lookup_token(department_code)
    normalized_department_name = _normalize_lookup_token(department_name)
    normalized_indication_code = _normalize_lookup_token(indication_code)
    normalized_indication_name = _normalize_lookup_token(indication_name)
    normalized_body_part_code = _normalize_lookup_token(body_part_code)
    normalized_body_part_name = _normalize_lookup_token(body_part_name)

    code_triplet = (
        normalized_department_code,
        normalized_indication_code,
        normalized_body_part_code,
    )
    if all(code_triplet):
        matched = reference_data.indication_catalog_by_code_triplet.get(code_triplet)
        if matched:
            return matched

    name_triplet = (
        normalized_department_name,
        normalized_indication_name,
        normalized_body_part_name,
    )
    if all(name_triplet):
        matched = reference_data.indication_catalog_by_name_triplet.get(name_triplet)
        if matched:
            return matched

    # 如果上游已经提供了科室信息，则必须三元精确匹配，不能再降级成“适应症+部位”
    # 的两元匹配，否则会把错误科室 silently 修正成唯一候选。
    code_pair = (normalized_indication_code, normalized_body_part_code)
    if not (normalized_department_code or normalized_department_name) and all(code_pair):
        candidates = reference_data.indication_catalog_by_code_pair_candidates.get(code_pair, ())
        if len(candidates) == 1:
            return candidates[0]

    name_pair = (normalized_indication_name, normalized_body_part_name)
    if not (normalized_department_code or normalized_department_name) and all(name_pair):
        candidates = reference_data.indication_catalog_by_name_pair_candidates.get(name_pair, ())
        if len(candidates) == 1:
            return candidates[0]

    return None


def normalize_standardized_indications_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    items = payload.get("items")
    normalized_items: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue

        department_code = _normalize_lookup_token(raw.get("department_code") or raw.get("dept_code"))
        department_name = _normalize_lookup_token(raw.get("department_name") or raw.get("dept_name"))
        indication_code = _normalize_lookup_token(raw.get("indication_code") or raw.get("code"))
        indication_name = _normalize_lookup_token(raw.get("indication_name") or raw.get("name") or raw.get("indication"))
        body_part_code = _normalize_lookup_token(raw.get("body_part_code") or raw.get("part_code"))
        body_part_name = _normalize_lookup_token(raw.get("body_part_name") or raw.get("body_part") or raw.get("part_name"))

        matched = resolve_indication_reference_item(
            department_code=department_code,
            department_name=department_name,
            indication_code=indication_code,
            indication_name=indication_name,
            body_part_code=body_part_code,
            body_part_name=body_part_name,
        )
        if matched is None:
            continue

        dedupe_key = (matched.indication_code, matched.body_part_code)
        if dedupe_key in seen_pairs:
            continue
        seen_pairs.add(dedupe_key)

        normalized_items.append(
            {
                **raw,
                "department_code": matched.department_code,
                "department_name": matched.department_name,
                "indication_code": matched.indication_code,
                "indication_name": matched.indication_name,
                "body_part_code": matched.body_part_code,
                "body_part_name": matched.body_part_name,
            }
        )

    summary = _normalize_lookup_token(payload.get("summary"))
    if not normalized_items:
        summary = "对话中未识别出可标准化的适应症"
    elif not summary:
        summary = "识别出{}项适应症：{}".format(
            len(normalized_items),
            "；".join(f"{item['indication_name']}（{item['body_part_name']}）" for item in normalized_items),
        )

    return {
        **payload,
        "summary": summary,
        "items": normalized_items,
    }


@lru_cache(maxsize=1)
def load_analysis_reference_data() -> AnalysisReferenceData:
    return _load_static_analysis_reference_data()
