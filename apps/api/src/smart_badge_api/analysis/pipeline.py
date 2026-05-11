"""分析管道：加载转写文件 → 调用 LLM → 返回结构化结果。

对于超长对话，自动分段分析后合并。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .consultation_evaluation import (
    rebuild_consultation_evaluation,
    rebuild_consultation_process_evaluation,
)
from .llm_client import chat_completion, parse_json_response
from .extraction_prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .reference_data import (
    normalize_standardized_indications_payload,
    resolve_indication_reference_item,
)
from .schemas import AnalysisResult
from .transcript import extract_transcript_segments, normalize_role, prepare_transcript
from smart_badge_api.tag_catalog_reference import (
    canonicalize_profile_tag_category,
    canonicalize_profile_tag_value,
    is_valid_profile_tag_value,
    load_tag_catalog_definitions,
)

logger = logging.getLogger(__name__)

# 超过此字符数的对话将自动分段处理；给系统提示词、字典和 JSON 输出留足余量。
_CHUNK_THRESHOLD = 10000
# 每段目标大小（字符），避免 prompt + 录音原文过长导致 LLM 响应不稳定。
_CHUNK_TARGET = 9000

_CONSULTATION_START_CUES = (
    "这次过来主要想了解什么项目",
    "这次过来主要了解什么项目",
    "这次主要想了解什么项目",
    "这次主要了解什么项目",
    "主要想了解什么项目",
    "主要了解什么项目",
    "今天主要是想了解哪方面",
    "今天主要想了解哪方面",
    "主要是想了解哪方面",
    "主要想了解哪方面",
    "今天主要是想咨询哪方面",
    "今天主要想咨询哪方面",
    "主要是想咨询哪方面",
    "主要想咨询哪方面",
    "想了解哪方面",
    "想咨询哪方面",
)

_PRIMARY_DEMAND_SEED_HINTS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("脸稍微紧一点，想让面部更紧致", "面部", ("脸稍微紧一点", "脸稍微紧", "紧一点", "更紧一点")),
    ("想做水光针", "面部", ("水光针", "水光")),
    ("希望做面部抗衰", "面部", ("抗衰",)),
    ("想做除皱注射", "面部", ("除皱", "打除皱", "除皱针")),
    ("想做薇旖美改善细纹", "面部", ("薇旖美",)),
)

_PRIMARY_DEMAND_ISSUE_HINTS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    (
        "调整眶外C线/眉尾轮廓，希望面部轮廓更自然协调",
        "眶外C线/眉尾",
        (
            "眶外C",
            "眶外c",
            "框外C",
            "框外c",
            "外框C",
            "外框c",
            "髋外C",
            "髋外c",
            "外科C",
            "外科c",
            "外方C",
            "外方c",
            "外方斜",
            "眉尾",
            "眉弓",
            "颞区",
        ),
    ),
    ("改善泪沟/眼周凹陷，希望恢复平整自然", "眼部", ("泪沟", "眼下凹", "眼下凹陷", "眼眶", "眼眶子", "卧蚕")),
    ("改善鼻基底/中面部衔接，希望恢复平整自然", "鼻基底/面中", ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "八字纹")),
    ("改善口周/唇部状态，希望自然轻度改善", "口周/唇部", ("口周", "嘴唇", "嘴巴", "唇部", "嘴角", "口下", "鼻基底", "仙人掌", "唇纹", "干瘪", "馒化")),
    ("改善面部松弛下垂", "面部", ("松弛下垂", "松弛", "下垂", "松垮", "往下走", "脸很垮", "脸也很垮", "脸垮", "很垮", "老态", "年轻")),
    ("想做拉皮修复，改善既往拉皮效果不佳", "面部", ("拉皮修复", "想要做修复", "做修复", "没有拉到", "又垮了", "又垮")),
    ("改善面部松弛/法令纹嘴角下垂，希望做拉皮提升", "面部", ("小拉皮", "拉皮", "面部提升", "提升手术", "法令纹", "嘴角下垂")),
    ("改善面部纹路和细纹", "面部", ("法令纹", "皱纹", "纹路", "干纹", "细纹", "薇旖美")),
    ("改善单眼皮，想让眼睛更有神", "眼部", ("单眼皮", "双眼皮", "眼睛没精神", "眼睛无神")),
    ("改善眼袋泪沟疲态", "眼部", ("眼袋", "泪沟")),
    ("改善肤色暗黄", "面部", ("暗黄", "黄气", "提亮")),
    ("改善毛孔粗大", "面部", ("毛孔",)),
    ("咨询鼻部塑形方案", "鼻部", ("鼻综合", "隆鼻", "鼻翼", "鼻头", "山根")),
    ("后背吸脂/超脂术，希望改善背部线条", "身体", ("后背", "背部", "小后背", "大后背", "超脂", "超脂术", "吸脂", "抽脂")),
    ("手部小疤痕，希望去除", "身体", ("手上有一块", "留的疤", "疤在哪", "疤痕")),
)
_PRIMARY_DEMAND_EXCLUDED_KEYWORDS: dict[str, tuple[str, ...]] = {
    "调整唇形，希望更自然精致": ("后期我觉得你", "你还有一点", "最主要是做", "口周抗衰", "下眼周抗衰", "我刚刚你进来", "你皮肤状态很好"),
    "改善口周/唇部状态，希望自然轻度改善": ("后期我觉得你", "你还有一点", "最主要是做", "口周抗衰", "下眼周抗衰", "我刚刚你进来", "你皮肤状态很好"),
    "改善面部松弛下垂": ("垮了", "垮成", "医院垮了", "鼻头", "鼻尖", "鼻小柱", "鼻子", "鼻部"),
    "改善面部纹路和细纹": ("她这样", "他这样", "案例", "像有些人", "我有时候说", "早点控制", "以后形成", "形成真性皱纹", "只有填胶原", "不然的话", "不好弄", "如果你在意", "你不在意", "不需要", "保留一点点纹路"),
    "改善眼袋泪沟疲态": ("做过眼袋", "刚做过眼袋", "前做过眼袋", "眼袋感觉还", "第1台眼袋", "第一台眼袋"),
    "手部小疤痕，希望去除": ("疤痕角", "瘢痕角", "疤痕体质", "疤痕体", "瘢痕体质", "瘢痕体"),
}
_PRIMARY_DEMAND_LOGISTICS_HINTS = (
    "一次性完成",
    "全部打",
    "不打了",
    "不做了",
    "分期",
    "利息",
    "支付",
    "路程",
    "太远",
)
_PLAN_CONTEXT_PRIMARY_DEMAND_KEYWORDS = (
    "鼻子",
    "鼻综合",
    "鼻头",
    "鼻尖",
    "鼻小柱",
    "山根",
    "自然",
    "手术痕迹",
    "脸会看起来小",
    "脸看起来小",
    "脸会小",
    "脸小",
    "瘦脸",
    "提升",
    "提拉",
    "紧致",
    "下颌缘",
    "轮廓",
    "液态提升",
    "英伦大提升",
    "肉毒",
    "肉毒素",
    "除皱",
    "乐提葆",
    "保妥适",
    "恒力",
    "后背",
    "背部",
    "小后背",
    "大后背",
    "吸脂",
    "抽脂",
    "超脂",
    "超脂术",
)

_BODY_PART_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("眶外C线（小O）", ("眶外C", "眶外c", "框外C", "框外c", "外框C", "外框c", "髋外C", "髋外c", "外科C", "外科c", "外方C", "外方c", "外方斜", "眉尾")),
    ("颞区（大O）", ("颞区", "太阳穴", "眉弓")),
    ("面部", ("面部", "中面部", "面中", "苹果肌", "法令纹", "嘴角囊带", "鼻基底", "鼻翼基底", "八字纹", "外轮廓线", "全脸", "脸")),
    ("眼部", ("眼部", "眼尾", "眼周", "眼睛", "双眼皮", "单眼皮", "泪沟", "卧蚕", "眼袋", "眶周", "眉下", "上眼睑", "眼型", "美杜莎")),
    ("鼻部", ("鼻部", "鼻子", "鼻综合", "山根", "鼻头", "鼻翼")),
    ("唇部（D）", ("唇部", "嘴唇", "嘴巴", "唇形", "唇纹", "唇珠", "丰唇", "口周", "嘴角", "口下")),
    ("颈部", ("颈部", "脖子", "颈纹")),
    ("胸部", ("胸部", "胸", "乳房")),
    ("身体", ("身体", "腰腹", "大腿", "手臂", "手部", "手上", "肩颈", "肩膀", "斜方肌", "后背", "背部", "小后背", "大后背", "富贵包")),
)

_INDICATION_HINTS: tuple[dict[str, Any], ...] = (
    # ── Y3 皮肤 ──
    {
        "department_name": "皮肤",
        "indication_name": "松弛下垂",
        "default_body_part": "面部",
        "keywords": ("松弛下垂", "松弛", "下垂", "松垮", "往下走", "脸很垮", "脸很很垮", "脸也很垮", "脸垮", "很垮", "老态", "年轻", "提升紧致", "面部提升", "紧致", "紧一点", "脸稍微紧", "收紧"),
        "excluded_keywords": ("垮了", "垮成", "医院垮了"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "纹路",
        "default_body_part": "面部",
        "keywords": ("法令纹", "皱纹", "纹路", "细纹", "抬头纹", "鱼尾纹", "川字纹", "颈纹", "唇纹", "薇旖美"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "暗黄",
        "default_body_part": "面部",
        "keywords": ("暗黄", "黄气", "提亮", "肤色暗沉", "肤色不均", "调肤色", "肤色均匀"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "毛孔",
        "default_body_part": "面部",
        "keywords": ("毛孔",),
    },
    {
        "department_name": "皮肤",
        "indication_name": "色斑",
        "default_body_part": "面部",
        "keywords": ("色斑", "雀斑", "黄褐斑", "晒斑", "祛斑", "祛班", "去斑", "去班", "雀班"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "敏感",
        "default_body_part": "面部",
        "keywords": ("敏感肌", "肌肤敏感", "敏感", "又敏"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "痤疮",
        "default_body_part": "面部",
        "keywords": ("痤疮", "痘痘", "痘印", "痘坑", "粉刺", "闭口", "祛痘"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "干燥",
        "default_body_part": "面部",
        "keywords": ("干燥", "缺水", "补水", "保湿", "水润", "又干", "皮肤干", "水光", "水光针", "童颜水光"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "红血丝",
        "default_body_part": "面部",
        "keywords": ("红血丝", "泛红"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "油脂旺盛",
        "default_body_part": "面部",
        "keywords": ("油脂旺盛", "出油", "油皮", "T区油", "T区出油", "面部比较油", "表面比较油"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "黑眼圈",
        "default_body_part": "眼部",
        "keywords": ("黑眼圈",),
    },
    {
        "department_name": "皮肤",
        "indication_name": "脱毛",
        "default_body_part": "身体",
        "keywords": ("脱毛", "腋毛", "腿毛"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "局部减脂",
        "default_body_part": "身体",
        "keywords": ("局部减脂", "瘦身", "溶脂"),
    },
    {
        "department_name": "皮肤",
        "indication_name": "疤痕",
        "default_body_part": "面部",
        "keywords": ("疤痕", "留的疤", "疤在哪", "手上有一块"),
        "excluded_keywords": ("疤痕体质", "疤痕体", "瘢痕体质", "瘢痕体", "体质"),
    },
    # ── Y1 外科 ──
    {
        "department_name": "外科",
        "indication_name": "眼袋",
        "default_body_part": "眼部",
        "keywords": ("眼袋", "泪沟", "内切", "眶隔释放", "眶隔脂肪"),
    },
    {
        "department_name": "外科",
        "indication_name": "双眼皮",
        "default_body_part": "眼部",
        "keywords": ("双眼皮", "重睑", "全切重睑", "单眼皮", "全切", "埋线", "开眼角", "三点定位", "眼型", "美杜莎", "大眼综合", "去皮去脂", "肌力矫正"),
    },
    {
        "department_name": "外科",
        "indication_name": "眼修复",
        "default_body_part": "眼部",
        "keywords": ("眼修复", "双眼皮修复", "重睑修复"),
    },
    {
        "department_name": "外科",
        "indication_name": "提眉",
        "default_body_part": "眼部",
        "keywords": ("提眉", "上睑下垂", "眉下切"),
    },
    {
        "department_name": "外科",
        "indication_name": "鼻综合",
        "default_body_part": "鼻部",
        "keywords": (
            "鼻综合", "隆鼻", "山根", "鼻头", "膨体", "假体隆鼻",
            "鼻部塑形", "鼻塑形", "鼻型", "改善鼻型", "做鼻子", "鼻部方案", "鼻整形",
            "驼峰鼻", "朝天鼻", "短鼻", "宽鼻", "Y鼻", "鼻背", "鼻尖", "筋膜包裹",
        ),
    },
    {
        "department_name": "外科",
        "indication_name": "鼻翼整形",
        "default_body_part": "鼻部",
        "keywords": ("鼻翼缩小", "鼻翼整形", "鼻翼宽", "鼻翼"),
        "excluded_keywords": ("鼻翼基底",),
    },
    {
        "department_name": "外科",
        "indication_name": "鼻修复",
        "default_body_part": "鼻部",
        "keywords": ("鼻修复", "鼻子修复", "取假体", "取膨体"),
    },
    {
        "department_name": "外科",
        "indication_name": "面部填充",
        "default_body_part": "面部",
        "keywords": (
            "面部填充", "苹果肌填充", "玻尿酸填充", "玻尿酸", "打玻尿酸",
            "法令纹填充", "嘴角填充", "下巴填充", "太阳穴填充",
            "太阳穴", "颞区", "丰额头", "下巴打玻尿酸", "注射填充", "填充塑形",
            "鼻基底", "面中填充", "鼻基底填充", "凹陷",
        ),
    },
    {
        "department_name": "微创",
        "indication_name": "塑美",
        "default_body_part": "唇部（D）",
        "keywords": (
            "卧蚕", "泪沟填充", "泪沟注射", "眼周填充", "眼下填充", "嗨体", "福曼",
            "眶外C线", "眶外C", "眶外c", "框外C", "框外c", "外框C", "外框c",
            "髋外C", "髋外c", "外科C", "外科c", "外方C", "外方c", "外方斜",
            "眉尾", "眉弓", "颞区",
            "瘦肩", "肩颈", "斜方肌", "斜方肌肉毒", "肉毒瘦肩", "瘦肩针",
            "口周", "嘴唇", "唇部", "唇形", "丰唇", "唇珠", "唇纹", "嘴角", "口下",
            "仙人掌", "注射嘴唇", "溶解", "溶解酶",
        ),
    },
    {
        "department_name": "外科",
        "indication_name": "面部除皱",
        "default_body_part": "面部",
        "keywords": ("面部除皱", "拉皮", "大拉皮", "提升手术", "中面部提升", "肉毒", "肉毒素", "除皱针", "瘦脸针"),
    },
    {
        "department_name": "外科",
        "indication_name": "面部吸脂",
        "default_body_part": "面部",
        "keywords": ("面部吸脂", "颊脂垫", "颊脂肪", "瘦脸吸脂", "颊脂去除"),
    },
    {
        "department_name": "外科",
        "indication_name": "假体下巴",
        "default_body_part": "面部",
        "keywords": ("假体下巴", "下巴假体", "硅胶下巴", "膨体下巴"),
    },
    {
        "department_name": "外科",
        "indication_name": "身体吸脂",
        "default_body_part": "身体",
        "keywords": ("身体吸脂", "腰腹吸脂", "大腿吸脂", "手臂吸脂", "后背吸脂", "背部吸脂", "抽脂", "吸脂", "超脂", "超脂术", "小后背", "大后背", "后背", "背部", "富贵包"),
    },
    {
        "department_name": "外科",
        "indication_name": "身体填充",
        "default_body_part": "身体",
        "keywords": ("身体填充", "自体脂肪填充", "脂肪填充", "丰臀"),
    },
    {
        "department_name": "外科",
        "indication_name": "隆胸",
        "default_body_part": "胸部",
        "keywords": ("隆胸", "假体隆胸", "假体丰胸", "自体脂肪丰胸", "丰胸", "曼托", "假体更换", "假体置换", "更换假体", "乳房假体", "光面圆形"),
    },
    {
        "department_name": "外科",
        "indication_name": "乳头整形",
        "default_body_part": "胸部",
        "keywords": ("乳头整形", "乳头内陷", "副乳头"),
    },
    {
        "department_name": "外科",
        "indication_name": "乳晕整形",
        "default_body_part": "胸部",
        "keywords": ("乳晕整形", "乳晕大", "乳晕缩小"),
    },
    {
        "department_name": "外科",
        "indication_name": "乳房下垂",
        "default_body_part": "胸部",
        "keywords": ("乳房下垂", "胸部下垂"),
    },
    # ── Y2 微创 ──
    {
        "department_name": "微创",
        "indication_name": "紧致淡纹",
        "default_body_part": "面部",
        "keywords": ("热玛吉", "超声刀", "超声炮", "超声抗衰", "黄金超声炮", "黄金炮", "黑钻", "热提拉", "热拉提", "射频", "抗衰", "紧致淡纹"),
    },
)


_STALE_FIRST_ITEM_SUMMARY_MARKERS = (
    "未识别",
    "无主诉",
    "无客户主诉",
    "无法形成主诉",
    "未提取",
    "仅进行机构介绍",
)

_NO_PRIOR_TREATMENT_TAG_PATTERNS = (
    r"(?:没|未|没有)(?:有)?做过(?:医美)?(?:项目|治疗|整形|抗衰保养)?",
    r"从(?:来)?没做过(?:医美)?(?:项目|治疗|整形|抗衰保养)?",
    r"(?:医美|医美项目|项目|治疗|整形|抗衰保养).{0,6}(?:就是)?第一次",
    r"第一次做(?:医美|医美项目|项目|治疗|整形|抗衰保养)",
    r"没有都没有哈，就是第一次",
)
_NO_PRIOR_TREATMENT_THIRD_PARTY_HINTS = ("老乡", "朋友", "同事", "别人", "人家", "案例", "顾客", "客人")
_NO_PRIOR_TREATMENT_REPORTING_HINTS = ("他说", "她说", "老乡说", "朋友说", "同事说", "别人说", "人家说")
_THIRD_PARTY_FACT_RELATION_HINTS = _NO_PRIOR_TREATMENT_THIRD_PARTY_HINTS + (
    "有些人",
    "客户",
    "姐妹",
    "闺蜜",
    "亲戚",
    "同学",
    "老公",
    "丈夫",
    "老婆",
    "妻子",
    "男朋友",
    "女朋友",
    "对象",
    "恋人",
    "妈妈",
    "母亲",
    "爸爸",
    "父母",
)
_THIRD_PARTY_FACT_REPORTING_HINTS = _NO_PRIOR_TREATMENT_REPORTING_HINTS + (
    "他说过",
    "她说过",
    "他说的",
    "她说的",
    "他当时",
    "她当时",
    "他今天",
    "她今天",
    "他后来",
    "她后来",
    "跟我说",
    "给我发消息",
    "发消息说",
)
_THIRD_PARTY_FACT_EXPERIENCE_HINTS = (
    "做过",
    "打过",
    "做了",
    "打了",
    "没做过",
    "没有做过",
    "第一次",
    "术后",
    "恢复",
    "不满意",
    "后悔",
    "翻车",
    "踩雷",
    "害怕",
    "纠结",
    "痘痘",
    "毛孔",
    "眼袋",
    "泪沟",
    "鼻子",
    "鼻背",
    "鼻翼",
    "鼻孔",
    "山根",
    "鼻头",
    "皮肤",
    "肤质",
    "法令纹",
    "纹路",
    "松弛",
    "疤痕",
    "微调",
    "嘴唇",
    "唇部",
    "口周",
    "凹陷",
)

_WECHAT_FOLLOW_UP_HINTS = (
    "加我微信",
    "加我的微信",
    "加您微信",
    "加你微信",
    "加微信",
    "加个微信",
    "加一个微信",
    "加下微信",
    "加一下微信",
    "先加微信",
    "先加个微信",
    "加企业微信",
    "企业微信",
    "微信联系",
    "微信沟通",
    "发微信",
    "给你二维码",
    "发你二维码",
    "我的微信",
    "随时跟我联系",
)
_INDICATION_SELF_REPORT_REQUIRED = frozenset({"提眉", "双眼皮", "紧致淡纹", "眼修复", "干燥"})
_INDICATION_INTENT_HINTS = ("想做", "想了解", "咨询", "考虑做", "打算做", "要做", "想要", "想补水", "主要想", "改善", "解决")
_INDICATION_QUESTION_HINTS = ("吗", "呢", "嘛", "是不是", "好吗", "可不可以", "行不行", "的话")
_INDICATION_SELF_REPORT_HINTS = (
    "我想",
    "我就想",
    "我想要",
    "我这个",
    "像我这种",
    "像我这个",
    "我本来",
    "我以前",
    "我主要",
    "我觉得",
    "我需要",
    "我要",
)
_SCAR_CONSTITUTION_HINTS = ("疤痕体质", "疤痕体", "瘢痕体质", "瘢痕体", "疤痕角", "瘢痕角")
_STAFF_EXPLANATORY_HINTS = (
    "建议",
    "适合",
    "恢复",
    "效果",
    "方案",
    "手术",
    "打针",
    "玻尿酸",
    "射频",
    "微针",
    "双眼皮",
    "提眉",
    "修复",
    "补水",
    "导入",
    "我们",
    "咱们",
    "给你",
    "你现在",
    "你的",
)
_STAFF_DEMO_HINTS = (
    "你看",
    "你说",
    "比如",
    "就像",
    "别人",
    "人家",
    "其他顾客",
    "其他客户",
    "有个顾客",
    "有个客户",
    "上午有个顾客",
    "今天上午有个顾客",
    "其他医院",
    "其他地方",
    "案例",
    "给我发微信",
    "发微信问我",
    "这个技术",
    "这个方案",
    "双眼皮的样子",
    "恢复很快",
)
_PRODUCT_EXPLANATION_TERMS = (
    "胶原水光",
    "胶原蛋白",
    "胶原",
    "水光",
    "玻尿酸",
    "肉毒",
    "瘦脸针",
    "除皱针",
    "光子",
    "黄金微针",
    "射频",
    "超声炮",
    "热玛吉",
    "童颜",
)
_STAFF_PRODUCT_EXPLANATION_CUES = (
    "有一种叫",
    "这种",
    "相当于",
    "主要是改善",
    "可以改善",
    "长期打",
    "一年打",
    "几支",
    "一支",
    "含量很少",
    "提取出来",
    "从牛身上",
    "从猪身上",
    "商城",
    "秒杀",
    "套餐",
)
_STAFF_SELF_EXAMPLE_CUES = (
    "像我的话",
    "我的话我",
    "我基本上",
    "我一般",
    "我自己一年",
    "我自己也",
    "我们自己员工",
    "我们员工",
    "我同事",
    "我们单位",
    "我给你看",
)
_STAFF_SELF_TREATMENT_OR_AGE_EXAMPLE_CUES = (
    "我要是",
    "要是我",
    "如果我",
    "假如我",
    "换成我",
    "像我",
    "我当时",
    "我那时候",
    "岁的时候",
    "像18岁",
    "像十八岁",
    "我不是00后",
    "我不是零零后",
    "我不是00后的",
    "我全脸都做",
    "我是全脸都做",
    "我是全脸做",
    "全脸都做完",
    "全脸做完",
    "抗衰我做到了极致",
    "我做到了极致",
    "我做医美的话",
    "我脸上鼻子",
    "我鼻子一万",
    "我鼻子花",
    "我鼻基底花",
    "鼻基底花了",
)
_STAFF_SCHEDULING_CUES = (
    "给你占",
    "手术位置",
    "排满了",
    "咨询师确定",
    "有没有要取消",
    "有取消的",
    "给你加进去",
    "协调时间",
)
_POSITIVE_HEALTH_SELF_REPORT_HINTS = ("我有", "我之前有", "有过", "一直有", "本身有")
_HEALTH_QUESTION_HINTS = ("有没有", "有吗", "是不是", "吗", "呢")
_FAMILY_DECISION_ACTION_STRONG_HINTS = ("商量", "考虑", "决定", "拍板", "同意", "出钱", "付款", "沟通", "确认", "让", "问问")

_PRIMARY_CUSTOMER_SIDE_ROLES = frozenset({"客户", "主客户", "访客"})
_COMPANION_SIDE_ROLES = frozenset({"同行人"})
_CUSTOMER_SIDE_ROLES = _PRIMARY_CUSTOMER_SIDE_ROLES | _COMPANION_SIDE_ROLES
_STAFF_SIDE_ROLES = frozenset({"咨询师", "医生", "工牌本人", "员工同事", "前台"})
_MISLABELED_CUSTOMER_CANDIDATE_ROLES = frozenset({"员工同事", "其他在场人员", "访客", "同行人"})
_MISLABELED_CUSTOMER_SELF_OR_DECISION_CUES = (
    "我",
    "我的",
    "我现在",
    "我钱",
    "我也",
    "我不要",
    "我不想",
    "我没有",
    "我没",
    "我那么远",
    "我过几天",
    "可不可以",
    "不可以",
    "不打了",
    "不做了",
    "怎么又多",
    "多了呀",
    "没有利息",
    "利息",
    "分期",
    "一次性支付",
    "刷信用卡",
    "支付宝",
    "划得来",
    "那么远",
    "跑过来",
    "赶时间",
    "回去",
    "弄不了",
    "麻烦",
    "余额",
    "钱别人借走",
)
_MISLABELED_CUSTOMER_INTERNAL_CUES = (
    "顾客可以",
    "客户可以",
    "我问一下",
    "你先做一下",
    "我先带他",
    "我先带她",
    "我带他",
    "我带她",
    "上班没有",
)
_MISLABELED_BADGE_OWNER_CUSTOMER_SPEECH_CUES = (
    "我只是想",
    "我希望",
    "我不希望",
    "我不想",
    "我担心",
    "我怕",
    "我害怕",
    "我老公",
    "我快",
    "我到了",
    "我能接受",
    "我不是专业",
    "我看懂",
    "让我看",
    "如果要做",
    "如果这样算",
    "我理解对了",
    "包括注射嘴唇",
    "要做几个",
    "要做3个",
    "要做三个",
    "打了以后",
    "打完以后",
)
_BADGE_OWNER_STAFF_SPEECH_CUES = (
    "我给你",
    "我帮你",
    "我带你",
    "我们医院",
    "我们这边",
    "我们医生",
    "我们客户",
    "我们的顾客",
    "我是咨询师",
    "我是医生",
)
_STAFF_CONFIRMATION_CUES = (
    "你",
    "您",
    "你这边",
    "您这边",
    "是不是",
    "是吧",
    "对吧",
    "有没有",
    "之前",
    "平时",
    "自己",
    "家里",
    "预算",
    "微信",
    "过敏",
    "疤痕体质",
    "第一次",
    "做过",
    "恢复期",
    "怕疼",
)
_CUSTOMER_CONFIRMATION_HINTS = (
    "对",
    "对的",
    "对啊",
    "对呀",
    "对对",
    "对对对",
    "是",
    "是的",
    "是啊",
    "是呀",
    "嗯",
    "嗯嗯",
    "没错",
    "可以",
    "行",
    "好",
    "有",
    "有的",
    "没有",
    "没有过",
    "没做过",
    "第一次",
    "是第一次",
    "都没有",
)
_CUSTOMER_CONFIRMATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(对|对的|对啊|对呀|对对|对对对)$"),
    re.compile(r"^(是|是的|是啊|是呀)$"),
    re.compile(r"^(嗯|嗯嗯|恩|恩恩|好|好的|行|可以|没错)$"),
    re.compile(r"^(有|有的|有过|没有|没有过|没做过|第一次|是第一次|都没有)$"),
)
_CUSTOMER_SELF_REPORT_HINTS = (
    "我想",
    "我就想",
    "我想要",
    "我觉得",
    "我现在",
    "我以前",
    "我之前",
    "我做过",
    "我打过",
    "我后悔",
    "我喜欢",
    "我不喜欢",
    "我担心",
    "我怕",
    "我主要",
    "我没有",
    "我有",
    "我是",
    "我今年",
)
_CUSTOMER_SELF_REPORT_FACT_HINTS = (
    "做过",
    "打过",
    "三年前",
    "几年",
    "不满意",
    "后悔",
    "喜欢",
    "不喜欢",
    "担心",
    "怕",
    "恢复",
    "预算",
    "价格",
    "诉求",
    "形状",
    "双眼皮",
    "眼袋",
    "法令纹",
    "纹路",
    "鼻",
)
_CUSTOMER_ELICITATION_HINTS = (
    "什么诉求",
    "有什么诉求",
    "什么想法",
    "有没有你喜欢",
    "什么时候做的",
    "做出来的时候满意吗",
    "你最喜欢哪一个",
    "你是在意",
    "你觉得",
    "你担心",
    "你怕",
    "你想",
    "你要",
    "要不要",
    "能不能",
    "可不可以",
    "行不行",
)
_STAFF_TECHNICAL_HINTS = (
    "双眼皮",
    "提眉",
    "恢复",
    "会肿",
    "玻尿酸",
    "胶原",
    "骨头",
    "眉尾",
    "眼尾",
    "切口",
    "手术",
    "医生",
    "方案",
)
_TREATMENT_HISTORY_CUES: tuple[str, ...] = (
    "以前",
    "之前",
    "原来",
    "做过",
    "打过",
    "做了",
    "打了",
    "术后",
    "恢复",
    "第一次",
    "没做过",
    "没有做过",
)
_LOCAL_CITY_SELF_REPORT_CUES: tuple[str, ...] = (
    "本地",
    "本地人",
    "成都人",
    "我是成都",
    "成都本地",
    "住成都",
    "我在成都",
    "住在成都",
    "一直在成都",
)
_NON_LOCAL_CITY_SELF_REPORT_CUES: tuple[str, ...] = (
    "外地",
    "外地人",
    "外地来的",
    "我不是本地的",
    "我不是成都的",
    "我不在成都",
    "我从外地来",
)

_BIRTHDATE_PATTERNS = (
    re.compile(r"((?:19|20)\d{2}年\d{1,2}月\d{1,2}[日号]?)"),
    re.compile(r"((?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2})"),
    re.compile(r"((?:19|20)\d{2}年\d{1,2}月)"),
    re.compile(r"((?:19|20)\d{2}[./-]\d{1,2})"),
    re.compile(r"((?:19|20)\d{2}年)"),
)
_AGE_VALUE_RE = re.compile(r"(?<!\d)(\d{2,3})\s*(多?岁)")
_AGE_VALUE_WITHOUT_SUFFIX_RE = re.compile(r"(?:身份证号年龄|年龄|今年多大|多大|几岁)[^，。；;]{0,10}?(?<!\d)(\d{2,3})(?!\d)")
_AGE_QUESTION_HINT_RE = re.compile(r"(?:今年)?(?:多大|几岁|多少岁)|年龄")
_AGE_EXAMPLE_OR_NEGATION_HINTS = (
    "不像",
    "不是",
    "不止",
    "不到",
    "没有",
    "案例",
    "顾客",
    "客户",
    "客人",
    "别人",
    "很多人",
    "人家",
    "朋友",
    "老乡",
    "他说",
    "她说",
    "开始做",
    "开始打",
    "等到",
    "到了",
)

_PRIMARY_DEMAND_CONCEPT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lip_perioral", ("口周", "嘴唇", "嘴巴", "唇部", "嘴角", "口下", "鼻基底", "唇形", "唇纹")),
    ("eye_bag_tear_trough_fatigue", ("眼袋", "泪沟", "疲态", "疲惫", "没精神")),
    ("eyelid_shape", ("双眼皮", "单眼皮", "眼型", "美杜莎")),
    ("facial_laxity", ("松弛", "下垂", "松垮", "下垮", "脸垮", "脸很垮", "脸也很垮", "很垮", "老态", "年轻", "提升", "紧致", "紧一点", "收紧", "抗衰")),
    ("wrinkle_texture", ("皱纹", "纹路", "细纹", "法令纹", "川字纹", "鱼尾纹")),
    ("skin_tone", ("暗黄", "黄气", "提亮", "肤色")),
    ("pores", ("毛孔",)),
    ("acne_marks_texture", ("痘印", "痘坑", "痘痘", "闭口", "肤质")),
    ("hydration", ("水光", "补水", "保湿", "缺水", "干燥")),
    ("nose_shape", ("鼻子", "鼻部", "鼻综合", "隆鼻", "山根", "鼻头", "鼻翼", "鼻孔", "鼻型")),
    ("body_liposuction", ("后背", "背部", "小后背", "大后背", "腰腹", "大腿", "手臂", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")),
    ("scar", ("疤痕", "疤", "留疤")),
)

_HEALTH_TAG_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("过敏史", ("过敏",)),
    ("疤痕体质", ("疤痕体质",)),
    ("备孕/妊娠/哺乳", ("备孕", "怀孕", "妊娠", "哺乳")),
    ("精神类疾病", ("精神类疾病", "精神病", "抑郁", "焦虑")),
    ("传染性疾病", ("传染", "乙肝", "丙肝", "梅毒", "艾滋")),
    ("高血压", ("高血压",)),
    ("糖尿病", ("糖尿病",)),
    ("心脑血管病", ("心脑血管", "心脏病", "冠心病", "脑梗", "脑血管")),
    ("免疫系统疾病", ("免疫系统疾病", "红斑狼疮", "免疫疾病")),
)
_NEGATIVE_HEALTH_HINTS = (
    "没有过敏",
    "不过敏",
    "没有疤痕体质",
    "没有备孕",
    "没备孕",
    "没怀孕",
    "没有怀孕",
    "没有哺乳",
    "没有高血压",
    "没有糖尿病",
)
_HEALTH_TOPIC_HINTS = (
    "过敏",
    "疤痕体质",
    "瘢痕体质",
    "备孕",
    "怀孕",
    "妊娠",
    "哺乳",
    "高血压",
    "糖尿病",
    "心脑血管",
    "心脏病",
    "冠心病",
    "脑梗",
    "免疫",
    "传染",
    "乙肝",
    "丙肝",
    "梅毒",
    "艾滋",
    "基础疾病",
    "禁忌",
    "风险",
)
_NON_HEALTH_NEGATION_CONTEXT_HINTS = (
    "存款",
    "预算",
    "钱",
    "费用",
    "价格",
    "付款",
    "父母拿钱",
    "刷卡",
    "贷款",
    "分期",
    "优惠",
)
_NEGATIVE_HEALTH_PATTERNS = (
    re.compile(r"(?:没有){1,3}(?:过敏|疤痕体质|备孕|怀孕|哺乳|高血压|糖尿病)"),
    re.compile(r"(?<!有)没有(?:过敏|疤痕体质|备孕|怀孕|哺乳|高血压|糖尿病)"),
    re.compile(r"(?<!有)没(?:备孕|怀孕|哺乳|高血压|糖尿病)"),
    re.compile(r"不过敏"),
    re.compile(r"(?:高血压|糖尿病|过敏|疤痕体质|备孕|怀孕|哺乳).{0,4}(?:没有|没)"),
    re.compile(r"(?:基础疾病|禁忌|风险).{0,8}(?:都)?(?:没有|没)"),
    re.compile(r"(?:没有|没).{0,8}(?:基础疾病|禁忌|风险)"),
)

_BROAD_TREATMENT_HISTORY_VALUES = frozenset({"手术类", "注射类", "光电类"})
_TREATMENT_HISTORY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("提眉", ("提眉", "提眉手术", "眉下切", "切眉")),
    ("双眼皮", ("双眼皮", "双眼皮手术", "重睑", "全切", "埋线双眼皮")),
    ("眼袋", ("眼袋", "祛眼袋", "去眼袋", "内切眼袋", "外切眼袋", "眶隔脂肪释放")),
    ("鼻综合", ("鼻综合", "隆鼻", "鼻修复", "鼻翼", "鼻头", "山根")),
    ("吸脂", ("吸脂", "抽脂")),
    ("拉皮", ("拉皮", "小拉皮", "大拉皮")),
    ("隆胸", ("隆胸", "丰胸")),
    ("线雕", ("线雕", "埋线", "埋线提升")),
    ("玻尿酸填充", ("玻尿酸", "玻尿酸填充")),
    ("脂肪填充", ("脂肪填充", "自体脂肪")),
    ("肉毒除皱/瘦脸", ("肉毒", "瘦脸针", "除皱针", "除皱瘦脸")),
    ("水光针", ("水光", "水光针")),
    ("中胚层治疗", ("中胚层", "中胚层治疗")),
    ("光子嫩肤", ("光子", "光子嫩肤")),
    ("热玛吉", ("热玛吉",)),
    ("超声炮", ("超声炮",)),
    ("射频/黄金微针", ("射频", "黄金微针", "热拉提")),
    ("手术类", ("双眼皮", "眼袋手术", "鼻综合", "隆鼻", "提眉", "吸脂", "拉皮", "隆胸", "手术", "外科整形", "假体", "膨体")),
    ("注射类", ("玻尿酸", "填充", "脂肪填充", "丰唇", "下巴", "太阳穴", "苹果肌", "肉毒", "瘦脸针", "除皱", "除皱瘦脸", "水光", "水光针", "中胚层", "注射", "薇旖美", "贝丽菲尔", "贝利菲尔", "菲利菲尔", "Bellafill", "填充塑形")),
    ("光电类", ("热玛吉", "超声炮", "光子", "激光", "射频", "热拉提", "黄金微针", "光电治疗")),
)
_INJECTION_HISTORY_QUESTION_HINTS = (
    "打过没有",
    "打过没",
    "打过吗",
    "有没有打过",
    "之前打过",
    "以前打过",
    "打过针",
    "打针",
    "注射",
    "瘦脸针",
    "除皱针",
    "肉毒",
)
_INJECTION_HISTORY_ANSWER_RE = re.compile(
    r"(?:我|嗯|对|是|以前|之前|原来|上次|那次|几年前|[一二两三四五六七八九十\d]+年前|[一二两三四五六七八九十\d]+年多前)?"
    r".{0,10}(?:打过(?:一次|一针|几次)?|(?:才|刚)?打了(?:要)?[一二两三四五六七八九十几\d]+个?月)"
)
_INJECTION_HISTORY_EXCLUDED_HINTS = (
    "打过电话",
    "打过来",
    "打过去",
    "打过招呼",
    "打过架",
    "打过光",
    "打过灯",
)
_INJECTION_HISTORY_CONTEXT_HINTS = (
    "针",
    "针剂",
    "注射",
    "玻尿酸",
    "肉毒",
    "瘦脸针",
    "除皱针",
    "水光",
    "水光针",
    "中胚层",
    "薇旖美",
    "贝丽菲尔",
    "贝利菲尔",
    "菲利菲尔",
    "Bellafill",
    "保妥适",
    "乐提葆",
    "乐提",
    "衡力",
    "恒力",
    "吉适",
    "集市",
    "英伦大提升",
    "液态提升",
    "单位",
    "抬头纹",
    "眉间",
    "咬肌",
    "下颌缘",
    "除皱",
    "瘦脸",
)
_ENERGY_HISTORY_CONTEXT_HINTS = (
    "点阵",
    "点阵激光",
    "激光",
    "光子",
    "光子嫩肤",
    "热玛吉",
    "超声炮",
    "射频",
    "黄金微针",
    "热拉提",
    "光电",
)
_HISTORY_DEVICE_HINTS = (
    "玻尿酸",
    "肉毒",
    "热玛吉",
    "超声炮",
    "光子",
    "水光",
    "乔雅登",
    "润致",
    "斐然",
    "海薇",
    "艾莉薇",
    "伊婉",
    "贝丽菲尔",
    "贝利菲尔",
    "菲利菲尔",
    "Bellafill",
    "濡白天使",
    "膨体",
    "假体",
)
_INJECTABLE_HISTORY_DEVICE_HINTS = (
    "玻尿酸",
    "肉毒",
    "水光",
    "乔雅登",
    "润致",
    "斐然",
    "海薇",
    "艾莉薇",
    "伊婉",
    "贝丽菲尔",
    "贝利菲尔",
    "菲利菲尔",
    "Bellafill",
    "濡白天使",
)
_NEGATIVE_PROJECT_CUES = ("不满意", "后悔", "失败", "翻车", "过敏", "踩雷", "做坏", "没做好", "效果不好")
_NEGATIVE_PROJECT_REPAIR_CUES = (
    "想调一下",
    "调一下",
    "调整一下",
    "想调整",
    "调整",
    "修复",
    "修一下",
    "重新做",
    "重做",
    "补救",
)
_NEGATIVE_PROJECT_VALUE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("双眼皮", ("双眼皮", "单眼皮", "眼皮")),
    ("提眉", ("提眉", "切眉", "眉下切")),
    ("眼袋", ("眼袋",)),
    ("鼻综合", ("鼻综合", "隆鼻", "鼻子", "鼻部")),
    ("玻尿酸", ("玻尿酸", "填充")),
    ("肉毒", ("肉毒", "除皱针", "瘦脸针")),
    ("水光", ("水光", "水光针")),
    ("光电", ("热玛吉", "超声炮", "光子", "激光", "射频", "黄金微针")),
)

_PRICE_SENSITIVITY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("高", ("太贵", "有点贵", "价格高", "预算有限", "烧钱", "便宜点", "能不能便宜", "性价比", "利息", "分期", "怎么又多", "多了", "有没有活动", "有没有那个活动", "有无活动", "优惠", "补贴", "一分钱", "少不了", "打折", "折扣")),
    ("中", ("价格中等", "中等一点", "价格合适", "划算")),
    ("低", ("价格不是问题", "不考虑价格", "预算充足")),
)

_CONCERN_HINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("价格类", "在意价格和预算", ("太贵", "有点贵", "价格高", "预算", "便宜点", "划算", "性价比", "利息", "分期", "一次性支付", "刷信用卡", "怎么又多", "多了")),
    ("效果类", "担心效果不够自然或不明显", ("自然", "效果", "明显", "维持多久", "太假", "不明显", "馒化", "变化太多", "不完美")),
    ("恢复类", "担心恢复期、肿胀或影响上班", ("恢复期", "恢复", "肿", "消肿", "上班", "请假", "拆线")),
    ("疼痛类", "担心疼痛或耐受度问题", ("疼", "痛", "怕疼", "疼不疼", "麻药")),
    ("风险类", "担心风险、副作用或安全性", ("风险", "副作用", "害怕", "过敏", "失败", "移位", "鼻基底最好不要动", "不好东西")),
    ("治疗安排类", "担心治疗安排不能一次完成或需要反复到院", ("全部打", "一次打", "一次性完成", "不打了", "不做了", "一个月再过来", "下次", "那么远", "跑过来", "赶时间", "过几天要回去")),
    ("决策类", "仍需考虑、商量或继续比较", ("考虑一下", "再考虑", "商量", "对比", "回去看一下", "回去看看")),
)

_DECISION_FACTOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("生理期", ("生理期", "经期", "月经", "大姨妈", "姨妈", "例假", "来事")),
    ("特殊身份", ("竞对同行", "竞品机构", "竞对机构", "同行机构", "黑名单", "黑名单人物")),
    ("支付/流程限制", ("支付失败", "付不了", "刷不了", "扫码不了", "下载不了", "验证码", "身份证", "流程")),
    ("时间/到院限制", ("赶时间", "路程远", "外地", "高铁", "飞机", "过几天要回去", "今天上班", "无法到院")),
    ("治疗条件限制", ("妊娠", "怀孕", "备孕", "哺乳", "禁忌", "不能做", "不能打", "不适合")),
)
_SPECIAL_IDENTITY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "竞对同行",
        (
            "我是同行",
            "我也是同行",
            "我们也是做医美",
            "我是做医美",
            "我在医美",
            "在医美上班",
            "在美容院上班",
            "在整形医院上班",
            "同行机构",
            "竞对机构",
            "竞品机构",
            "竞对",
            "同行",
        ),
    ),
    ("黑名单", ("黑名单", "黑名单人物", "被拉黑", "拉黑名单")),
)
_FAMILY_DECISION_RELATION_HINTS: tuple[str, ...] = (
    "老公",
    "男朋友",
    "女朋友",
    "对象",
    "父母",
    "爸妈",
    "家里",
    "家人",
    "丈夫",
    "老婆",
)
_FAMILY_DECISION_ACTION_HINTS: tuple[str, ...] = ("商量", "考虑", "决定", "拍板", "同意", "出钱", "付款", "问一下", "沟通", "确认")
_DECISION_FACTOR_FROM_CONCERN_HINTS: dict[str, tuple[str, ...]] = {
    "价格": ("价格", "预算", "太贵", "有点贵", "便宜", "性价比", "划算"),
    "恢复期": ("恢复", "恢复期", "肿", "消肿", "上班", "请假", "拆线"),
    "效果": ("效果", "自然", "不明显", "明显", "维持多久", "太假"),
    "疼痛": ("疼", "痛", "怕疼", "麻药", "耐受"),
    "风险": ("风险", "副作用", "安全", "过敏", "失败"),
    "时间/到院限制": ("赶时间", "路程", "外地", "高铁", "飞机", "反复到院", "无法到院", "过几天要回去"),
    "支付/流程限制": ("支付", "付不了", "刷不了", "扫码", "下载", "流程", "身份证"),
    "治疗条件限制": ("禁忌", "不能做", "不能打", "不适合", "怀孕", "备孕", "哺乳", "生理期", "过敏"),
}

_PLAN_HINTS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("口周整体联合注射", "口周/唇部/鼻基底", ("口周整体", "口周", "下面部", "鼻基底", "联合注射", "3ml", "三毫升")),
    ("唇部残留溶解后少量塑形", "唇部", ("嘴唇", "唇部", "唇形", "溶解", "溶掉", "少打", "少量", "增加一点", "克制")),
    ("唇部玻尿酸填充", "唇部", ("单做唇", "唇部单选", "唇部玻尿酸", "嘴唇", "唇部", "1ml", "一毫升")),
    ("薇旖美", "面部", ("薇旖美",)),
    ("水光针", "面部", ("水光",)),
    ("光子嫩肤", "面部", ("光子嫩肤", "光子")),
    ("热玛吉/超声抗衰", "面部", ("热玛吉", "超声炮", "热拉提", "射频")),
    ("鼻部局部玻尿酸/再生材料支撑调整", "鼻部", ("鼻小柱", "鼻尖", "鼻头", "鼻背", "鼻基底", "鼻坎基底", "鼻部", "鼻子", "定彩", "瑞德喜", "芭比", "再生", "玻尿酸")),
    ("玻尿酸填充塑形", "面部", ("玻尿酸", "填充", "斐然", "润致", "乔雅登", "海薇", "艾莉薇", "伊婉", "濡白天使")),
    ("肉毒/除皱瘦脸", "面部", ("肉毒", "瘦脸针", "除皱")),
    ("胶原/胶原蛋白泪沟填充", "眼部", ("泪沟", "胶原", "胶原蛋白", "胶原针")),
    ("提眉联合双眼皮", "眼部", ("提眉加双眼皮", "提眉和双眼皮", "提眉双眼皮")),
    ("双眼皮", "眼部", ("双眼皮", "全切", "埋线")),
    ("眼袋/眶隔脂肪释放", "眼部", ("眼袋", "眶隔", "眶隔脂肪", "内切")),
    ("提眉", "眼部", ("提眉", "眉下切")),
    ("鼻综合", "鼻部", ("鼻综合", "隆鼻", "鼻翼", "鼻头", "山根", "膨体", "假体")),
    ("面部填充", "面部", ("脂肪填充", "苹果肌", "太阳穴填充", "法令纹填充", "丰唇", "小圆唇")),
    ("吸脂塑形", "身体", ("吸脂", "抽脂", "腰腹", "大腿", "手臂", "后背", "背部", "小后背", "大后背", "超脂", "超脂术", "富贵包")),
)
_PLAN_RECOMMENDATION_CUES = ("建议", "适合", "推荐", "可以考虑", "先做", "先打", "打除皱", "下次", "更适合", "方案", "从", "选择")
_RECOMMENDATION_EVIDENCE_CUES = (
    "建议",
    "推荐",
    "适合",
    "可以考虑",
    "先做",
    "先打",
    "可以做",
    "可以打",
    "能做",
    "能打",
    "方案",
    "选择",
    "联合",
    "配合",
    "设计",
    "安排",
    "可以配",
    "配两支",
    "配一支",
    "一起做",
    "你就买",
    "买那个",
    "直接买",
    "先买",
    "购买",
    "给你做",
    "给你打",
    "医生建议",
    "要打",
    "结合",
    "至少",
    "支的量",
    "性价比",
    "少打",
    "少量",
    "增加一点",
    "克制",
    "溶解",
    "溶掉",
)

_DEAL_SUCCESS_HINTS = (
    "付款",
    "付钱",
    "交钱",
    "刷卡",
    "定金",
    "意向金",
    "下单",
    "开单",
    "排手术",
    "排队做项目",
    "安排治疗",
    "今天做",
    "验券",
    "核销",
    "对公码",
    "微信或支付宝扫",
    "微信支付宝扫",
    "敷麻药",
    "放在你账户上",
    "送光子嫩肤",
)
_DEAL_PENDING_HINTS = (
    "考虑一下",
    "再考虑",
    "回去看一下",
    "回去看看",
    "回去商量",
    "商量一下",
    "跟老公商量",
    "跟家里商量",
    "和家里商量",
    "和老公商量",
    "再对比",
    "再决定",
    "以后再说",
    "先不做",
)
_DEAL_PRICE_LOSS_HINTS = ("太贵", "有点贵", "价格高", "预算不够", "烧钱", "利息", "分期", "怎么又多", "多了")
_DEAL_SCHEDULE_LOSS_HINTS = ("上班", "请假", "没时间", "时间不合适", "改天", "下次", "赶时间", "那么远", "跑过来", "过几天要回去")
_DEAL_EFFECT_LOSS_HINTS = ("效果", "自然", "恢复期", "风险", "害怕")
_DEAL_SUCCESS_COMPLETED_PATTERNS = (
    re.compile(r"(?:已经|已|刚|都|先|就)?(?:付款|付钱|交钱|刷卡|下单|开单|验券|核销)(?:成功|完成|了|过)?"),
    re.compile(r"(?:付了|交了|刷了|下了|开了).{0,6}(?:钱|款|卡|单|定金|意向金)?"),
    re.compile(r"(?:定金|意向金).{0,6}(?:已经|已|都|先|就)?(?:交了|付了|收了|到了|交过|付过)"),
    re.compile(r"(?:已经|已|都|刚).{0,8}(?:安排治疗|安排手术|安排项目|做治疗|做项目|敷麻药)"),
)
_DEAL_SUCCESS_COMMITMENT_PATTERNS = (
    re.compile(r"(?:我|那我|行|可以|好).{0,8}(?:先|就)?(?:交|付).{0,6}(?:定金|意向金)"),
    re.compile(r"(?:我|那我|行|可以|好).{0,8}(?:刷卡|付款|下单|开单)"),
)
_DEAL_SUCCESS_FLOW_ONLY_HINTS = (
    "对公码",
    "微信或支付宝扫",
    "微信支付宝扫",
    "付款方式",
    "怎么付款",
    "怎么付",
    "可以刷卡",
    "能刷卡",
    "今天做的话",
    "要做的话",
    "如果今天做",
    "送光子嫩肤",
)
_DEAL_HYPOTHETICAL_HINTS = ("如果", "要是", "假如", "的话")

_MONEY_TEXT_RE = re.compile(
    r"((?:\d+(?:\.\d+)?万(?:[到至~-]\d+(?:\.\d+)?万)?|\d{3,6}(?:[到至~-]\d{3,6})?(?:元|块|w|W)?))"
)


def _call_llm_json(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 12000,
    attempts: int = 3,
) -> dict:
    last_error: Exception | None = None
    current_max_tokens = max_tokens
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            response_text = chat_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=current_max_tokens,
            )
            return parse_json_response(response_text)
        except Exception as exc:
            last_error = exc
            if "finish_reason=length" in str(exc):
                current_max_tokens = min(max(current_max_tokens * 2, 12_000), 20_000)
            if attempt >= max(attempts, 1):
                raise
            logger.warning(
                "LLM JSON parse failed attempt=%d/%d max_tokens=%d: %s; retrying",
                attempt,
                max(attempts, 1),
                current_max_tokens,
                exc,
            )
    raise RuntimeError("LLM JSON parsing failed") from last_error


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _clean_text(item))]
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    return []


def _dedupe_text_list(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _clean_text(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _ms_to_mmss(ms: int) -> str:
    total_seconds = max(ms, 0) // 1000
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _segment_evidence(segment: dict[str, Any]) -> str:
    text = _clean_text(segment.get("text"))
    return f"[{_ms_to_mmss(int(segment.get('begin', 0) or 0))}] {text}" if text else ""


def _consultation_segments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return extract_transcript_segments(raw)


def _find_consultation_start_index(segments: list[dict[str, Any]]) -> int:
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if any(cue in text for cue in _CONSULTATION_START_CUES):
            return index
    return 0


def _candidate_body_parts(text: str, default_body_part: str | None = None) -> list[str]:
    matched: list[str] = []
    for body_part, keywords in _BODY_PART_HINTS:
        if any(keyword in text for keyword in keywords):
            matched.append(body_part)
    if default_body_part and any(keyword in default_body_part for keyword in ("唇", "口周", "嘴")):
        default_body_part = "唇部（D）"
    if default_body_part and default_body_part not in matched:
        matched.append(default_body_part)
    return matched or ([default_body_part] if default_body_part else [])


def _primary_demand_concepts(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return set()
    return {
        concept
        for concept, keywords in _PRIMARY_DEMAND_CONCEPT_HINTS
        if any(keyword in compact for keyword in keywords)
    }


def _primary_demand_body_part(item: dict[str, Any]) -> str:
    body_part = _clean_text(item.get("body_part"))
    if body_part:
        return body_part
    demand = _clean_text(item.get("demand"))
    candidates = _candidate_body_parts(demand)
    return candidates[0] if candidates else ""


def _primary_demand_item_score(item: dict[str, Any]) -> tuple[int, int, int, int]:
    demand = _clean_text(item.get("demand"))
    evidence = _clean_text(item.get("evidence"))
    priority = item.get("priority")
    try:
        normalized_priority = int(priority)
    except (TypeError, ValueError):
        normalized_priority = 999
    return (
        1 if evidence else 0,
        len(_primary_demand_concepts(demand)),
        len(demand),
        -normalized_priority,
    )


def _primary_demand_items_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_compact = re.sub(r"[；，。、“”‘’（）()：:、,.\s]+", "", _clean_text(left.get("demand")))
    right_compact = re.sub(r"[；，。、“”‘’（）()：:、,.\s]+", "", _clean_text(right.get("demand")))
    if not left_compact or not right_compact:
        return False
    if left_compact == right_compact:
        return True

    if (
        (("法令纹" in left_compact and "鼻基底" in right_compact) or ("鼻基底" in left_compact and "法令纹" in right_compact))
        and _looks_like_nasolabial_base_solution_mechanism(
            " ".join(
                part
                for part in (
                    _clean_text(left.get("evidence")),
                    _clean_text(right.get("evidence")),
                    _clean_text(left.get("demand")),
                    _clean_text(right.get("demand")),
                )
                if part
            )
        )
    ):
        return True

    shared_anchor_groups = (
        ("鼻基底", "口下", "口周", "嘴角", "嘴唇", "唇部", "唇形"),
        ("眼袋", "泪沟", "眼下", "疲态", "疲惫"),
        ("山根", "鼻背", "鼻头", "鼻尖", "鼻翼", "鼻型"),
        ("后背", "背部", "小后背", "大后背", "吸脂", "超脂"),
    )
    for anchors in shared_anchor_groups:
        if any(anchor in left_compact for anchor in anchors) and any(anchor in right_compact for anchor in anchors):
            return True

    left_body_part = _primary_demand_body_part(left)
    right_body_part = _primary_demand_body_part(right)
    if left_body_part and right_body_part and left_body_part != right_body_part:
        return False

    left_concepts = _primary_demand_concepts(_clean_text(left.get("demand")))
    right_concepts = _primary_demand_concepts(_clean_text(right.get("demand")))
    if left_concepts and right_concepts and left_concepts.intersection(right_concepts):
        return True

    return left_compact in right_compact or right_compact in left_compact


def _dedupe_primary_demand_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    kept: list[dict[str, Any]] = []
    changed = False
    for item in items:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(kept)
                if _primary_demand_items_overlap(existing, item)
            ),
            None,
        )
        if duplicate_index is None:
            kept.append(item)
            continue

        changed = True
        if _primary_demand_item_score(item) > _primary_demand_item_score(kept[duplicate_index]):
            kept[duplicate_index] = item
    return kept, changed


def _append_primary_demand(
    items: list[dict[str, Any]],
    *,
    demand: str,
    body_part: str | None,
    evidence: str,
) -> None:
    normalized_demand = _naturalize_primary_demand(demand, body_part=body_part, evidence=evidence)
    normalized_body_part = _naturalize_primary_body_part(body_part, evidence=evidence)
    if not normalized_demand:
        return
    if not evidence:
        return
    dedupe_key = (normalized_demand, _clean_text(normalized_body_part))
    existing_keys = {
        (_clean_text(item.get("demand")), _clean_text(item.get("body_part")))
        for item in items
        if isinstance(item, dict)
    }
    if dedupe_key in existing_keys or any(
                _primary_demand_items_overlap(item, {"demand": normalized_demand, "body_part": normalized_body_part})
        for item in items
        if isinstance(item, dict)
    ):
        return
    items.append(
        {
            "priority": len(items) + 1,
            "demand": normalized_demand,
            "body_part": normalized_body_part,
            "evidence": evidence,
        }
    )


def _naturalize_primary_body_part(body_part: str | None, *, evidence: str) -> str | None:
    normalized = _clean_text(body_part)
    compact_evidence = re.sub(r"\s+", "", _clean_text(evidence))
    if any(keyword in compact_evidence for keyword in ("后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")):
        return "身体"
    if "法令纹" in compact_evidence and _looks_like_nasolabial_base_solution_mechanism(compact_evidence):
        return normalized or "面部"
    if any(keyword in compact_evidence for keyword in ("脸很垮", "脸很很垮", "脸也很垮", "脸垮")):
        return "面部"
    if any(keyword in compact_evidence for keyword in ("鼻基底", "鼻翼基底", "鼻子底", "鼻底")) and not any(keyword in compact_evidence for keyword in ("嘴唇", "唇部", "唇形", "唇纹")):
        return "鼻基底/面中"
    return normalized or body_part


def _naturalize_primary_demand(demand: str, *, body_part: str | None, evidence: str) -> str:
    normalized = _clean_text(demand)
    evidence_text = _clean_text(evidence)
    context = " ".join(part for part in (normalized, _clean_text(body_part), evidence_text) if part)
    compact = re.sub(r"\s+", "", context)
    if not normalized:
        return ""

    if any(keyword in compact for keyword in ("后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")):
        if "富贵包" in compact:
            return "后背/富贵包吸脂塑形，希望改善背部线条"
        return "后背吸脂/超脂术，希望改善背部线条"

    if any(keyword in compact for keyword in ("脸稍微紧一点", "脸稍微紧", "脸紧一点", "紧一点", "收紧一点")):
        return "脸稍微紧一点，想让面部更紧致"
    if any(keyword in compact for keyword in ("脸很垮", "脸很很垮", "脸也很垮", "脸垮")):
        if "眼睛" in compact and "老态" in compact:
            return "脸部下垮、眼睛显老态，希望整体改善"
        if "年轻" in compact:
            return "脸部下垮，希望改善松弛感、看起来更年轻"
        return "脸部下垮，希望改善松弛感"

    evidence_compact = re.sub(r"\s+", "", evidence_text)
    if _primary_demand_is_wrinkle_texture(normalized) and "法令纹" in evidence_compact and not any(
        keyword in evidence_compact for keyword in ("面部纹路", "细纹", "皱纹", "抬头纹", "鱼尾纹", "川字纹")
    ):
        return "解决法令纹问题"

    if any(keyword in context for keyword in _LIP_CONTEXT_HINTS):
        if "鼻基底" in compact and any(keyword in compact for keyword in ("凹陷", "凹", "低")):
            return "鼻基底比较凹陷，希望改善面中支撑"
        if any(keyword in compact for keyword in ("残留", "没有形态", "没形态", "溶解", "溶掉")):
            return "唇部有残留、形态不佳，希望先溶解再少量调整"
        if any(keyword in compact for keyword in ("唇纹", "干瘪", "不饱满")):
            return "唇部干瘪或唇纹明显，希望更饱满自然"
        if any(keyword in compact for keyword in ("口下", "鼻基底", "衔接", "仙人掌")):
            return "改善口下/鼻基底衔接，希望口周更自然"
        if "嘴角" in compact:
            return "改善嘴角和口周线条，希望更自然"
        if any(keyword in compact for keyword in ("口周抗衰", "唇周", "白唇", "人中", "口周")):
            return "改善口周衔接和干瘪感，希望更自然"
        if any(keyword in compact for keyword in ("唇形", "小圆唇")):
            return "调整唇形，希望更自然精致"
        return normalized

    if "眼袋" in compact:
        if any(keyword in compact for keyword in ("泪沟", "凹陷", "凹的地方")):
            return "改善眼袋和眼下凹陷，希望眼下更平整"
        if any(keyword in compact for keyword in ("水肿", "肿")):
            return "改善眼袋和水肿感，希望眼下更清爽"
        return "改善眼袋问题，希望眼下更平整"

    if (
        any(keyword in compact for keyword in ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "苹果肌", "八字纹"))
        and any(keyword in compact for keyword in ("凹", "空", "平整", "衔接", "填充", "瑞德喜", "玻尿酸"))
        and not _looks_like_rhinoplasty_indication_statement(evidence_text)
    ):
        return "改善鼻基底/中面部衔接，希望恢复平整自然"

    if any(keyword in compact for keyword in ("鼻子", "鼻部", "鼻头", "鼻尖", "鼻小柱", "山根", "鼻背")):
        if any(keyword in compact for keyword in ("山根", "鼻背", "低")):
            return "山根/鼻背偏低，希望整体调整鼻型"
        if any(keyword in compact for keyword in ("鼻孔", "鼻尖", "鼻头")):
            return "改善鼻尖/鼻头细节，希望鼻型更协调"
        return "咨询鼻部塑形，希望鼻型更自然协调"

    return normalized


def _should_naturalize_existing_primary_demand(
    demand: str,
    *,
    evidence: str,
    original_evidence: str = "",
) -> bool:
    normalized = _clean_text(demand)
    evidence_compact = re.sub(r"\s+", "", _clean_text(evidence))
    if not normalized or not evidence_compact:
        return False

    original_evidence_text = _clean_text(original_evidence)
    if (
        original_evidence_text
        and original_evidence_text != _clean_text(evidence)
        and _looks_like_staff_product_explanation_or_self_example(original_evidence_text)
        and _looks_like_direct_customer_primary_demand_line(evidence_compact, _keywords_for_primary_demand(normalized))
    ):
        return True

    # Existing LLM demands should keep their natural wording. Only correct the
    # common over-generalizations where a specific demand was broadened into a
    # nearby project family.
    if (
        any(keyword in normalized for keyword in ("鼻部塑形", "鼻型", "鼻部方案"))
        and any(keyword in evidence_compact for keyword in ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "苹果肌", "八字纹"))
        and any(keyword in evidence_compact for keyword in ("凹", "空", "平整", "衔接", "填充", "瑞德喜", "玻尿酸"))
        and not _looks_like_rhinoplasty_indication_statement(evidence_compact)
    ):
        return True

    return (
        _primary_demand_is_wrinkle_texture(normalized)
        and "法令纹" in evidence_compact
        and not any(keyword in evidence_compact for keyword in ("面部纹路", "细纹", "皱纹", "抬头纹", "鱼尾纹", "川字纹"))
    )


def _backfill_primary_demands(payload: dict[str, Any], *, segments: list[dict[str, Any]]) -> bool:
    start_index = _find_consultation_start_index(segments)
    main_segments = segments[start_index:]
    existing_items = [dict(item) for item in _as_list(payload.get("items")) if isinstance(item, dict)]
    items: list[dict[str, Any]] = existing_items[:3]
    if len(items) >= 3:
        return False

    # 先优先提取客户开场时直接表达的“想做/想了解”类主诉。
    for segment_index, segment in enumerate(main_segments[:10], start=start_index):
        if len(items) >= 3:
            break
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        for demand, body_part, keywords in _PRIMARY_DEMAND_SEED_HINTS:
            if any(keyword in text for keyword in keywords):
                supported = _supported_fact_source(segments, segment_index, keywords=keywords)
                if supported is None:
                    continue
                if not _looks_like_primary_demand_evidence(supported[1], demand=demand):
                    continue
                before_count = len(items)
                _append_primary_demand(items, demand=demand, body_part=body_part, evidence=supported[1])
                if len(items) == before_count:
                    continue
                if len(items) >= 3:
                    break

    # 再用正式咨询中的痛点描述补齐主诉。
    for segment_index, segment in enumerate(main_segments, start=start_index):
        if len(items) >= 3:
            break
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        for demand, body_part, keywords in _PRIMARY_DEMAND_ISSUE_HINTS:
            if any(keyword in text for keyword in keywords):
                supported = _supported_fact_source(
                    segments,
                    segment_index,
                    keywords=keywords,
                    excluded_keywords=_PRIMARY_DEMAND_EXCLUDED_KEYWORDS.get(demand, ()),
                )
                if supported is None:
                    continue
                if not _looks_like_primary_demand_evidence(supported[1], demand=demand):
                    continue
                before_count = len(items)
                _append_primary_demand(items, demand=demand, body_part=body_part, evidence=supported[1])
                if len(items) == before_count:
                    continue
                if len(items) >= 3:
                    break

    if not items:
        eyelid_demand_evidence = _infer_eyelid_shape_demand_evidence(main_segments)
        if eyelid_demand_evidence:
            _append_primary_demand(
                items,
                demand="改善双眼皮形态",
                body_part="眼部",
                evidence=eyelid_demand_evidence,
            )

    if not items and _allows_main_fact_floor(segments):
        for segment_index, segment in enumerate(main_segments, start=start_index):
            text = _clean_text(segment.get("text"))
            if not text or not _is_staff_side_segment(segment):
                continue
            for demand, body_part, keywords in _PRIMARY_DEMAND_SEED_HINTS + _PRIMARY_DEMAND_ISSUE_HINTS:
                if not any(keyword in text for keyword in keywords):
                    continue
                supported = _supported_fact_source(
                    segments,
                    segment_index,
                    keywords=keywords,
                    excluded_keywords=_PRIMARY_DEMAND_EXCLUDED_KEYWORDS.get(demand, ()),
                    allow_weak_staff_inference=True,
                )
                if supported is None:
                    continue
                evidence = supported[1]
                if not _looks_like_primary_demand_evidence(
                    evidence,
                    demand=demand,
                    allow_weak_staff_inference=True,
                ):
                    continue
                _append_primary_demand(items, demand=demand, body_part=body_part, evidence=evidence)
                break
            if items:
                break

    if not items:
        return False
    if len(items) == len(existing_items) and all(
        _clean_text(item.get("evidence")) for item in existing_items
    ):
        return False

    payload["items"] = items
    payload["summary"] = "；".join(_clean_text(item.get("demand")) for item in items if _clean_text(item.get("demand")))
    return True


def _first_plan_context_item(result_dict: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    recommendations = _as_list(_as_dict(result_dict.get("staff_recommendations")).get("items"))
    indications = _as_list(_as_dict(result_dict.get("standardized_indications")).get("items"))
    recommendation = next((item for item in recommendations if isinstance(item, dict)), None)
    indication = next((item for item in indications if isinstance(item, dict)), None)
    return recommendation, indication


def _plan_context_text(recommendation: dict[str, Any] | None, indication: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if isinstance(recommendation, dict):
        parts.extend(
            _clean_text(recommendation.get(key))
            for key in ("recommendation", "product_or_solution", "body_part", "evidence")
        )
    if isinstance(indication, dict):
        parts.extend(
            _clean_text(indication.get(key))
            for key in ("indication_name", "body_part_name", "evidence")
        )
    return " ".join(part for part in parts if part)


def _infer_primary_demand_from_plan_context(
    recommendation: dict[str, Any] | None,
    indication: dict[str, Any] | None,
    *,
    evidence: str,
) -> tuple[str, str | None]:
    context = " ".join(
        part
        for part in (
            _plan_context_text(recommendation, indication),
            _clean_text(evidence),
        )
        if part
    )
    body_part = (
        _clean_text((recommendation or {}).get("body_part"))
        or _clean_text((indication or {}).get("body_part_name"))
        or None
    )
    if any(keyword in context for keyword in ("后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")):
        if "富贵包" in context:
            return "后背/富贵包吸脂塑形，希望改善背部线条", "身体"
        return "后背吸脂/超脂术，希望改善背部线条", "身体"
    if any(keyword in context for keyword in ("液态提升", "英伦大提升", "下颌缘", "脸会看起来小", "脸看起来小", "瘦脸", "轮廓", "提拉", "提升")):
        return "改善面部轮廓，希望提升紧致、脸看起来更小", body_part or "面部"
    if any(keyword in context for keyword in ("除皱", "肉毒", "肉毒素", "抬头纹", "眉间纹", "鱼尾纹")):
        return "改善面部皱纹，希望纹路更平整", body_part or "面部"
    if any(keyword in context for keyword in ("玻尿酸", "填充", "凹陷", "苹果肌", "太阳穴", "下巴", "唇")):
        return f"改善{body_part or '面部'}凹陷或轮廓，希望填充塑形", body_part or "面部"
    if any(keyword in context for keyword in ("水光", "补水", "干燥", "缺水")):
        return "改善皮肤干燥，希望补水保湿", body_part or "面部"
    if any(keyword in context for keyword in ("鼻综合", "鼻子", "鼻部", "鼻头", "鼻尖", "鼻小柱", "山根", "手术痕迹")):
        return "咨询鼻部塑形方案，希望鼻型自然精致", body_part or "鼻部"

    plan = _clean_text((recommendation or {}).get("recommendation")) or _clean_text(
        (recommendation or {}).get("product_or_solution")
    )
    indication_name = _clean_text((indication or {}).get("indication_name"))
    topic = plan or indication_name or "医美改善"
    return f"咨询{topic}方案", body_part


def _find_plan_context_primary_demand_evidence(
    segments: list[dict[str, Any]],
    *,
    recommendation: dict[str, Any] | None,
    indication: dict[str, Any] | None,
) -> str | None:
    context = _plan_context_text(recommendation, indication)
    matched_context_keywords = tuple(
        keyword
        for keyword in _PLAN_CONTEXT_PRIMARY_DEMAND_KEYWORDS
        if keyword in context
    )
    context_keywords = tuple(dict.fromkeys(matched_context_keywords + _PLAN_CONTEXT_PRIMARY_DEMAND_KEYWORDS))

    start_index = _find_consultation_start_index(segments)
    main_segments = segments[start_index:]

    # 优先找客户侧或疑似客户侧的“效果目标/追问”短句。
    for segment in main_segments:
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if not any(keyword in text for keyword in context_keywords):
            continue
        is_customer_like = _is_customer_side_segment(segment)
        if (
            is_customer_like
            or (
                not _is_staff_side_segment(segment)
                and not _is_badge_owner_segment(segment)
                and any(cue in text for cue in ("是不是", "吗", "吧", "要瘦", "脸会", "脸看起来"))
            )
        ):
            evidence = _segment_evidence(segment)
            if evidence:
                return evidence

    for item in (recommendation, indication):
        if not isinstance(item, dict):
            continue
        evidence = _clean_text(item.get("evidence"))
        if evidence and any(keyword in evidence for keyword in context_keywords):
            return evidence

    # 弱内容录音兜底：客户发言少但已进入具体方案/价格/项目讲解时，
    # 用方案讲解本身承载主诉推断证据。
    for segment in main_segments:
        text = _clean_text(segment.get("text"))
        if not text or len(text) < 8:
            continue
        if any(keyword in text for keyword in context_keywords):
            evidence = _segment_evidence(segment)
            if evidence:
                return evidence

    for item in (recommendation, indication):
        if not isinstance(item, dict):
            continue
        evidence = _clean_text(item.get("evidence"))
        if evidence:
            return evidence
    return None


def _backfill_primary_demands_from_plan_context(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    payload = _as_dict(result_dict.setdefault("customer_primary_demands", {}))
    recommendation, indication = _first_plan_context_item(result_dict)
    if recommendation is None and indication is None:
        return False
    existing_items = [item for item in _as_list(payload.get("items")) if isinstance(item, dict)]
    if existing_items:
        inference_note = _clean_text(payload.get("inference_note"))
        if len(existing_items) > 1 or "弱内容录音兜底" not in inference_note:
            return False

    segments = _consultation_segments(raw)
    if not segments:
        return False

    evidence = _find_plan_context_primary_demand_evidence(
        segments,
        recommendation=recommendation,
        indication=indication,
    )
    if not evidence:
        return False
    if _looks_like_third_party_narrative_statement(evidence):
        return False

    demand, body_part = _infer_primary_demand_from_plan_context(
        recommendation,
        indication,
        evidence=evidence,
    )
    if _primary_demand_is_wrinkle_texture(demand) and not _looks_like_explicit_wrinkle_texture_primary_demand(evidence):
        return False
    payload["items"] = [
        {
            "priority": 1,
            "demand": demand,
            "body_part": body_part,
            "evidence": evidence,
        }
    ]
    payload["summary"] = demand
    note = _clean_text(payload.get("inference_note"))
    fallback_note = "弱内容录音兜底：客户发言较少，依据已形成的推荐方案、适应症和上下文补提主诉"
    payload["inference_note"] = f"{note}；{fallback_note}" if note and fallback_note not in note else note or fallback_note

    recommendations = _as_dict(result_dict.get("staff_recommendations"))
    rec_items = [item for item in _as_list(recommendations.get("items")) if isinstance(item, dict)]
    rec_changed = False
    for item in rec_items:
        priorities = [
            int(value)
            for value in _as_list(item.get("demand_priority"))
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
        ]
        if priorities and all(priority == 1 for priority in priorities):
            continue
        item["demand_priority"] = [1]
        rec_changed = True
    if rec_changed:
        recommendations["items"] = rec_items
        result_dict["staff_recommendations"] = recommendations

    result_dict["customer_primary_demands"] = payload
    _sync_chief_complaint_primary_demands(result_dict)
    return True


def _find_first_matching_segment(
    segments: list[dict[str, Any]],
    *,
    keywords: tuple[str, ...],
) -> dict[str, Any] | None:
    for segment_index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if text and any(keyword in text for keyword in keywords):
            return segment
    return None


def _append_standardized_indication_item(
    items: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    seen_indication_codes: set[str],
    *,
    hint: dict[str, Any],
    body_part_candidates: list[str],
    evidence: str,
) -> bool:
    if not evidence:
        return False
    indication_name = _clean_text(hint.get("indication_name"))
    if indication_name in {"眼袋", "纹路", "敏感", "干燥", "鼻综合"} and not _looks_like_indication_evidence(
        indication_name,
        evidence,
    ):
        return False
    excluded_keywords = tuple(hint.get("excluded_keywords", ()))
    if excluded_keywords and _text_contains_excluded_keyword(evidence, excluded_keywords):
        return False
    for body_part_name in body_part_candidates:
        matched = resolve_indication_reference_item(
            department_name=hint.get("department_name"),
            indication_name=hint["indication_name"],
            body_part_name=body_part_name,
        )
        if matched is None:
            continue
        if matched.indication_code in seen_indication_codes:
            continue
        dedupe_key = (matched.indication_code, matched.body_part_code)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        seen_indication_codes.add(matched.indication_code)
        items.append(
            {
                "department_code": matched.department_code,
                "department_name": matched.department_name,
                "indication_code": matched.indication_code,
                "indication_name": matched.indication_name,
                "body_part_code": matched.body_part_code,
                "body_part_name": matched.body_part_name,
                "evidence": evidence,
            }
        )
        return True
    return False


def _primary_demand_has_high_confidence_indication_phrase(demand_text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(demand_text))
    if not compact:
        return False
    high_confidence_terms = (
        "全切重睑",
        "大眼综合",
        "去皮去脂",
        "肌力矫正",
        "曼托光面圆形假体更换",
        "假体更换",
        "假体置换",
        "童颜水光",
    )
    return any(term in compact for term in high_confidence_terms)


def _standardized_indication_items_from_primary_demands(
    primary_demand_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(primary_demand_payload, dict):
        return []

    items: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_indication_codes: set[str] = set()
    for demand_item in _as_list(primary_demand_payload.get("items")):
        if len(items) >= 3:
            break
        if not isinstance(demand_item, dict):
            continue
        demand_text = _clean_text(demand_item.get("demand"))
        body_part = _clean_text(demand_item.get("body_part"))
        evidence = _clean_text(demand_item.get("evidence"))
        if not demand_text or not evidence:
            continue
        if not _looks_like_primary_demand_evidence(
            evidence,
            demand=demand_text,
            allow_weak_staff_inference=True,
        ) and not _primary_demand_has_high_confidence_indication_phrase(demand_text):
            continue

        context = f"{demand_text} {body_part} {evidence}"
        for hint in _INDICATION_HINTS:
            keywords = tuple(hint.get("keywords", ()))
            if not _text_contains_any_keyword(context, keywords):
                continue
            body_part_candidates = _candidate_body_parts(context, body_part or hint.get("default_body_part"))
            if _append_standardized_indication_item(
                items,
                seen_keys,
                seen_indication_codes,
                hint=hint,
                body_part_candidates=body_part_candidates,
                evidence=evidence,
            ):
                break
    return items


def _recommendation_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("recommendation"),
        item.get("product_or_solution"),
        item.get("body_part"),
        item.get("evidence"),
    ]
    return " ".join(_clean_text(part) for part in parts if _clean_text(part))


def _recommendation_evidence_for_indication(
    indication_name: str,
    recommendations_payload: dict[str, Any] | None,
) -> str | None:
    if not isinstance(recommendations_payload, dict):
        return None
    hint = _indication_hint_for_name(indication_name)
    keywords = tuple(hint.get("keywords", ())) if hint else (indication_name,)
    for recommendation in _as_list(recommendations_payload.get("items")):
        if not isinstance(recommendation, dict):
            continue
        recommendation_text = _recommendation_text(recommendation)
        if not recommendation_text or not _text_contains_any_keyword(recommendation_text, keywords):
            continue
        evidence = _clean_text(recommendation.get("evidence"))
        if evidence:
            return evidence
    return None


def _append_reference_indication_item(
    items: list[dict[str, Any]],
    *,
    department_name: str,
    indication_name: str,
    body_part_name: str,
    evidence: str,
) -> bool:
    matched = resolve_indication_reference_item(
        department_name=department_name,
        indication_name=indication_name,
        body_part_name=body_part_name,
    )
    if matched is None:
        return False
    dedupe_key = (matched.indication_code, matched.body_part_code)
    for existing in items:
        if (
            _clean_text(existing.get("indication_code")),
            _clean_text(existing.get("body_part_code")),
        ) == dedupe_key:
            return False
    items.append(
        {
            "department_code": matched.department_code,
            "department_name": matched.department_name,
            "indication_code": matched.indication_code,
            "indication_name": matched.indication_name,
            "body_part_code": matched.body_part_code,
            "body_part_name": matched.body_part_name,
            "evidence": evidence,
        }
    )
    return True


def _recommendation_evidence_matching(
    recommendations_payload: dict[str, Any] | None,
    predicate: Callable[[str], bool],
) -> str | None:
    if not isinstance(recommendations_payload, dict):
        return None
    for recommendation in _as_list(recommendations_payload.get("items")):
        if not isinstance(recommendation, dict):
            continue
        recommendation_text = _recommendation_text(recommendation)
        if not recommendation_text or not predicate(recommendation_text):
            continue
        evidence = _clean_text(recommendation.get("evidence"))
        if evidence:
            return evidence
    return None


def _looks_like_face_filler_context(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    area_terms = (
        "鼻基底",
        "鼻翼基底",
        "鼻子底",
        "鼻底",
        "中面部",
        "面中",
        "苹果肌",
        "八字纹",
        "外轮廓线",
        "眶外C",
        "法令纹",
        "太阳穴",
        "面颊",
        "凹陷",
        "凹了",
        "空了",
        "发空",
    )
    treatment_terms = ("填充", "注射", "打", "玻尿酸", "瑞德喜", "胶原", "支撑型", "复配", "一支", "几支")
    return any(term in compact for term in area_terms) and any(term in compact for term in treatment_terms)


def _augment_high_confidence_indications(
    items: list[dict[str, Any]],
    *,
    segments: list[dict[str, Any]],
    primary_items: list[dict[str, Any]],
    staff_recommendations_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    primary_context = "\n".join(_primary_demand_item_context(item) for item in primary_items)
    recommendation_context = ""
    if isinstance(staff_recommendations_payload, dict):
        recommendation_context = "\n".join(
            _recommendation_text(item)
            for item in _as_list(staff_recommendations_payload.get("items"))
            if isinstance(item, dict)
        )
    combined_context = f"{primary_context}\n{recommendation_context}"

    if _looks_like_face_filler_context(combined_context):
        evidence = _find_supported_evidence_for_keywords(
            segments,
            keywords=("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "苹果肌", "八字纹", "外轮廓线"),
        ) or _recommendation_evidence_matching(
            staff_recommendations_payload,
            _looks_like_face_filler_context,
        )
        if evidence:
            changed = _append_reference_indication_item(
                items,
                department_name="外科",
                indication_name="面部填充",
                body_part_name="面部",
                evidence=evidence,
            ) or changed

    if _looks_like_eye_injection_plastic_context(combined_context):
        evidence = _find_supported_evidence_for_keywords(
            segments,
            keywords=("嗨体", "福曼", "复配", "卧蚕套餐"),
        ) or _recommendation_evidence_matching(
            staff_recommendations_payload,
            _looks_like_eye_injection_plastic_context,
        )
        if evidence:
            changed = _append_reference_indication_item(
                items,
                department_name="微创",
                indication_name="塑美",
                body_part_name="眼部（D）",
                evidence=evidence,
            ) or changed

    return items, changed


def _recommendation_family(text: str) -> str:
    normalized = _clean_text(text)
    if not normalized:
        return ""
    if any(keyword in normalized for keyword in ("超声炮", "超声刀", "热玛吉", "热拉提", "射频", "超声抗衰", "光电抗衰", "提升紧致")):
        return "光电抗衰"
    if any(keyword in normalized for keyword in ("唇", "嘴唇", "嘴巴", "口周", "溶解", "溶解酶")):
        return "唇部溶解塑形"
    if any(keyword in normalized for keyword in ("除皱", "肉毒", "瘦脸针")):
        return "除皱瘦脸"
    if any(keyword in normalized for keyword in ("水光", "中胚")):
        return "中胚层/水光"
    if "泪沟" in normalized and any(keyword in normalized for keyword in ("胶原", "胶原蛋白", "胶原针", "填充")):
        return "泪沟填充"
    if any(keyword in normalized for keyword in ("玻尿酸", "填充")):
        return "填充塑形"
    if any(keyword in normalized for keyword in ("鼻综合", "隆鼻", "鼻子", "鼻部", "膨体", "假体", "肋软骨")):
        return "鼻综合"
    if "提眉" in normalized and "双眼皮" in normalized:
        return "提眉/双眼皮联合"
    if any(keyword in normalized for keyword in ("双眼皮", "全切", "埋线")):
        return "双眼皮"
    if any(keyword in normalized for keyword in ("眼袋", "眶隔", "泪沟")):
        return "眼袋"
    if any(keyword in normalized for keyword in ("提眉", "眉下切")):
        return "提眉"
    if any(keyword in normalized for keyword in ("吸脂", "抽脂", "腰腹", "大腿", "手臂", "后背", "背部", "小后背", "大后背", "超脂", "超脂术", "富贵包")):
        return "吸脂塑形"
    return normalized


def _find_recommendation_indication_evidence(
    segments: list[dict[str, Any]],
    *,
    item: dict[str, Any],
    hint: dict[str, Any],
) -> str | None:
    keywords = tuple(hint.get("keywords", ()))
    excluded_keywords = tuple(hint.get("excluded_keywords", ()))
    existing_evidence = _clean_text(item.get("evidence"))
    if existing_evidence and _text_contains_any_keyword(existing_evidence, keywords):
        return existing_evidence

    start_index = _find_consultation_start_index(segments)
    for segment in segments[start_index:]:
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if not _text_contains_any_keyword(text, keywords):
            continue
        if excluded_keywords and _text_contains_excluded_keyword(text, excluded_keywords):
            continue
        evidence = _segment_evidence(segment)
        if evidence:
            return evidence
    return existing_evidence


def _backfill_standardized_indications_from_recommendations(
    items: list[dict[str, Any]],
    seen_keys: set[tuple[str, str]],
    seen_indication_codes: set[str],
    *,
    segments: list[dict[str, Any]],
    recommendations_payload: dict[str, Any] | None,
) -> bool:
    if not isinstance(recommendations_payload, dict):
        return False
    changed = False
    for recommendation in _as_list(recommendations_payload.get("items")):
        if not isinstance(recommendation, dict):
            continue
        recommendation_text = _recommendation_text(recommendation)
        if not recommendation_text:
            continue
        for hint in _INDICATION_HINTS:
            keywords = tuple(hint.get("keywords", ()))
            if not _text_contains_any_keyword(recommendation_text, keywords):
                continue
            evidence = _find_recommendation_indication_evidence(
                segments,
                item=recommendation,
                hint=hint,
            )
            body_part = _clean_text(recommendation.get("body_part"))
            body_part_candidates = _candidate_body_parts(
                recommendation_text,
                body_part or hint.get("default_body_part"),
            )
            changed = _append_standardized_indication_item(
                items,
                seen_keys,
                seen_indication_codes,
                hint=hint,
                body_part_candidates=body_part_candidates,
                evidence=evidence or "",
            ) or changed
            break
    return changed


def _backfill_standardized_indications(
    payload: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    primary_demand_payload: dict[str, Any] | None = None,
    staff_recommendations_payload: dict[str, Any] | None = None,
) -> bool:
    existing_items: list[dict[str, Any]] = []
    if _as_list(payload.get("items")):
        normalized = normalize_standardized_indications_payload(payload)
        existing_items = [dict(item) for item in _as_list(normalized.get("items")) if isinstance(item, dict)]

    start_index = _find_consultation_start_index(segments)
    main_segments = segments[start_index:]
    items: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_indication_codes: set[str] = set()
    for item in existing_items:
        indication_code = _clean_text(item.get("indication_code"))
        body_part_code = _clean_text(item.get("body_part_code"))
        dedupe_key = (indication_code, body_part_code)
        if indication_code and body_part_code and dedupe_key not in seen_keys:
            seen_keys.add(dedupe_key)
            seen_indication_codes.add(indication_code)
            items.append(item)

    if not existing_items:
        for segment_index, segment in enumerate(main_segments, start=start_index):
            if len(items) >= 3:
                break
            text = _clean_text(segment.get("text"))
            if not text:
                continue
            for hint in _INDICATION_HINTS:
                if not any(keyword in text for keyword in hint["keywords"]):
                    continue
                supported = _supported_fact_source(
                    segments,
                    segment_index,
                    keywords=hint["keywords"],
                    excluded_keywords=tuple(hint.get("excluded_keywords", ())),
                )
                if supported is None:
                    continue
                evidence = supported[1]
                if not _looks_like_indication_evidence(hint["indication_name"], evidence):
                    continue
                for body_part_name in _candidate_body_parts(text, hint.get("default_body_part")):
                    matched = resolve_indication_reference_item(
                        department_name=hint.get("department_name"),
                        indication_name=hint["indication_name"],
                        body_part_name=body_part_name,
                    )
                    if matched is None:
                        continue
                    # 这条兜底逻辑的目标是“补出最核心的适应症”，而不是把同一适应症
                    # 扩成多个部位。否则在长段咨询里很容易因为多个部位关键词共现，
                    # 把同一适应症重复挂到眼部/面部/颈部等多个部位上。
                    if matched.indication_code in seen_indication_codes:
                        continue
                    dedupe_key = (matched.indication_code, matched.body_part_code)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    seen_indication_codes.add(matched.indication_code)
                    items.append(
                        {
                            "department_code": matched.department_code,
                            "department_name": matched.department_name,
                            "indication_code": matched.indication_code,
                            "indication_name": matched.indication_name,
                            "body_part_code": matched.body_part_code,
                            "body_part_name": matched.body_part_name,
                            "evidence": evidence,
                        }
                    )
                    break

    if len(items) < 3 and isinstance(primary_demand_payload, dict):
        primary_items = _as_list(primary_demand_payload.get("items"))
        for demand_item in primary_items:
            if len(items) >= 3:
                break
            if not isinstance(demand_item, dict):
                continue
            demand_text = _clean_text(demand_item.get("demand"))
            demand_body_part = _clean_text(demand_item.get("body_part"))
            if not demand_text:
                continue
            before_demand_count = len(items)
            for hint in _INDICATION_HINTS:
                if not any(keyword in demand_text for keyword in hint["keywords"]):
                    continue
                body_part_candidates = _candidate_body_parts(demand_text, demand_body_part or hint.get("default_body_part"))
                supported = None
                for segment_index, segment in enumerate(main_segments, start=start_index):
                    text = _clean_text(segment.get("text"))
                    if not text:
                        continue
                    if any(keyword in text for keyword in hint["keywords"]):
                        supported = _supported_fact_source(
                            segments,
                            segment_index,
                            keywords=hint["keywords"],
                            excluded_keywords=tuple(hint.get("excluded_keywords", ())),
                        )
                    elif demand_text in text or (demand_body_part and demand_body_part in text):
                        supported = _supported_fact_source(
                            segments,
                            segment_index,
                            keywords=(demand_text, demand_body_part) if demand_body_part else (demand_text,),
                            excluded_keywords=tuple(hint.get("excluded_keywords", ())),
                        )
                    if supported is not None:
                        break
                if supported is None:
                    continue
                evidence = supported[1]
                if not _looks_like_indication_evidence(hint["indication_name"], evidence):
                    continue
                for body_part_name in body_part_candidates:
                    matched = resolve_indication_reference_item(
                        department_name=hint.get("department_name"),
                        indication_name=hint["indication_name"],
                        body_part_name=body_part_name,
                    )
                    if matched is None:
                        continue
                    dedupe_key = (matched.indication_code, matched.body_part_code)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    seen_indication_codes.add(matched.indication_code)
                    items.append(
                        {
                            "department_code": matched.department_code,
                            "department_name": matched.department_name,
                            "indication_code": matched.indication_code,
                            "indication_name": matched.indication_name,
                            "body_part_code": matched.body_part_code,
                            "body_part_name": matched.body_part_name,
                            "evidence": evidence,
                        }
                    )
                    break
                if len(items) > before_demand_count:
                    break
            if len(items) >= 3:
                break

    _backfill_standardized_indications_from_recommendations(
        items,
        seen_keys,
        seen_indication_codes,
        segments=segments,
        recommendations_payload=staff_recommendations_payload,
    )

    # ---- Staff-only 兜底：如果前面两轮都没匹配到，放宽为只要员工/医生发言
    # 中包含适应症关键词且该段落有实质内容（>20字），就作为证据。
    # 这处理了客户几乎不说话、全靠面诊推进的场景。
    if not items and _allows_main_fact_floor(
        segments,
        staff_recommendations_payload=staff_recommendations_payload,
    ):
        for segment_index, segment in enumerate(main_segments, start=start_index):
            text = _clean_text(segment.get("text"))
            if not text or len(text) < 20:
                continue
            if not _is_staff_side_segment(segment):
                continue
            for hint in _INDICATION_HINTS:
                if not any(keyword in text for keyword in hint["keywords"]):
                    continue
                excluded = tuple(hint.get("excluded_keywords", ()))
                if excluded and _text_contains_excluded_keyword(text, excluded):
                    continue
                evidence = _segment_evidence(segment)
                if not evidence:
                    continue
                if not _looks_like_indication_evidence(
                    hint["indication_name"],
                    evidence,
                    allow_weak_staff_inference=True,
                ):
                    continue
                for body_part_name in _candidate_body_parts(text, hint.get("default_body_part")):
                    matched = resolve_indication_reference_item(
                        department_name=hint.get("department_name"),
                        indication_name=hint["indication_name"],
                        body_part_name=body_part_name,
                    )
                    if matched is None:
                        continue
                    if matched.indication_code in seen_indication_codes:
                        continue
                    dedupe_key = (matched.indication_code, matched.body_part_code)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    seen_indication_codes.add(matched.indication_code)
                    items.append(
                        {
                            "department_code": matched.department_code,
                            "department_name": matched.department_name,
                            "indication_code": matched.indication_code,
                            "indication_name": matched.indication_name,
                            "body_part_code": matched.body_part_code,
                            "body_part_name": matched.body_part_name,
                            "evidence": evidence,
                        }
                    )
                    break

    primary_items_for_collapse = [
        item
        for item in _as_list(primary_demand_payload.get("items")) if isinstance(item, dict)
    ] if isinstance(primary_demand_payload, dict) else []
    items, _collapsed_changed = _collapse_nasolabial_fold_related_indications(items, primary_items_for_collapse)

    normalized = normalize_standardized_indications_payload(
        {
            "summary": "",
            "items": items,
            "inference_note": payload.get("inference_note"),
        }
    )
    if not _as_list(normalized.get("items")):
        return False
    payload.clear()
    payload.update(normalized)
    return _collapsed_changed or len(_as_list(normalized.get("items"))) != len(existing_items)


def _backfill_first_consultation_item(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    segments = _consultation_segments(raw)
    if not segments:
        return False

    primary_demands = _as_dict(result_dict.setdefault("customer_primary_demands", {}))
    standardized_indications = _as_dict(result_dict.setdefault("standardized_indications", {}))

    primary_changed = _backfill_primary_demands(primary_demands, segments=segments)
    indications_changed = _backfill_standardized_indications(
        standardized_indications,
        segments=segments,
        primary_demand_payload=primary_demands,
        staff_recommendations_payload=_as_dict(result_dict.get("staff_recommendations")),
    )

    if primary_changed:
        result_dict["customer_primary_demands"] = primary_demands
        _sync_chief_complaint_primary_demands(result_dict)
    if indications_changed:
        result_dict["standardized_indications"] = standardized_indications
        _sync_chief_complaint_standardized_indications(result_dict)
    return primary_changed or indications_changed


def _clear_stale_first_item_summary(result_dict: dict[str, Any]) -> None:
    consultation_result = _as_dict(result_dict.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    summary = _clean_text(chief.get("summary"))
    primary_demands = _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
    has_primary_demands = bool(primary_demands)
    stale_by_phrase = summary and any(marker in summary for marker in _STALE_FIRST_ITEM_SUMMARY_MARKERS)
    stale_by_semantics = has_primary_demands and summary and (
        ("主诉" in summary and ("无" in summary or "没有" in summary))
        or "仅有咨询师" in summary
        or "咨询师对" in summary
    )
    if stale_by_phrase or stale_by_semantics:
        chief["summary"] = ""
        consultation_result["chief_complaint_and_indications"] = chief
        result_dict["consultation_result"] = consultation_result


def _profile_weight_by_category() -> dict[str, int]:
    return {
        item.name: int(item.weight_level)
        for item in load_tag_catalog_definitions()
        if item.weight_level is not None
    }


def _normalized_segment_role(segment: dict[str, Any]) -> str:
    return normalize_role(_clean_text(segment.get("role")))


def _normalized_segment_business_role(segment: dict[str, Any]) -> str:
    return normalize_role(_clean_text(segment.get("speaker_business_role")))


def _normalized_segment_label(segment: dict[str, Any]) -> str:
    return normalize_role(_clean_text(segment.get("speaker_label")))


def _companion_statement_refers_to_primary_customer(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(cue in compact for cue in ("我自己", "我本人", "我当时", "我要是", "如果我", "像我", "我们员工", "我们自己")):
        return False
    return any(
        cue in compact
        for cue in (
            "她想",
            "他想",
            "她要",
            "他要",
            "她担心",
            "他担心",
            "她怕",
            "他怕",
            "她做过",
            "他做过",
            "她打过",
            "他打过",
            "她之前",
            "他之前",
            "她主要",
            "他主要",
            "她这个",
            "他这个",
            "陪她",
            "陪他",
            "带她",
            "带他",
            "替她问",
            "替他问",
            "帮她问",
            "帮他问",
        )
    )


def _is_companion_side_segment(segment: dict[str, Any]) -> bool:
    return bool(
        {
            _normalized_segment_role(segment),
            _normalized_segment_business_role(segment),
            _normalized_segment_label(segment),
        }
        & _COMPANION_SIDE_ROLES
    )


def _is_customer_side_segment(segment: dict[str, Any]) -> bool:
    roles = {
        _normalized_segment_role(segment),
        _normalized_segment_business_role(segment),
        _normalized_segment_label(segment),
    }
    if roles & _PRIMARY_CUSTOMER_SIDE_ROLES:
        return True
    if roles & _COMPANION_SIDE_ROLES:
        return _companion_statement_refers_to_primary_customer(_clean_text(segment.get("text")))
    return False


def _is_staff_side_segment(segment: dict[str, Any]) -> bool:
    return (
        _normalized_segment_role(segment) in _STAFF_SIDE_ROLES
        or _normalized_segment_business_role(segment) in _STAFF_SIDE_ROLES
        or _normalized_segment_label(segment) in _STAFF_SIDE_ROLES
    )


_LOW_PARTICIPATION_THRESHOLD = 0.15
_customer_ratio_cache: dict[int, float] = {}


def _customer_segment_ratio(segments: list[dict[str, Any]]) -> float:
    """客户发言占比（按片段数）。对同一 segments 列表做 id 级缓存。"""
    key = id(segments)
    cached = _customer_ratio_cache.get(key)
    if cached is not None:
        return cached
    _customer_ratio_cache.clear()
    ratio = sum(1 for s in segments if _is_customer_side_segment(s)) / len(segments) if segments else 0.0
    _customer_ratio_cache[key] = ratio
    return ratio


def _looks_like_doctor_role_customer_speech(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(
        cue in compact
        for cue in (
            "我建议你",
            "我建议您",
            "我给你",
            "我给您",
            "我帮你",
            "我帮您",
            "我带你",
            "我带您",
            "我们医生",
            "我们医院",
            "我们这边",
        )
    ):
        return False
    customer_cues = (
        "我这种",
        "我的情况",
        "我的脸",
        "我脸",
        "我皮肤",
        "我害怕",
        "我就害怕",
        "我担心",
        "我就怕",
        "我怕",
        "我不想",
        "我不要",
        "我不能接受",
        "我接受",
        "我选择",
        "我来",
        "我咨询",
        "我做过",
        "我打过",
        "我第一次",
        "我预算",
        "我当时预估",
        "给我退",
        "把钱退",
        "全退",
        "性价比",
        "下颌线",
        "脸下垂",
        "肉多下垂",
        "囊袋",
    )
    return any(cue in compact for cue in customer_cues)


def _looks_like_direct_customer_primary_demand_line(text: str, keywords: tuple[str, ...] = ()) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if _looks_like_staff_self_treatment_or_age_example(compact):
        return False
    if _looks_like_staff_extra_problem_or_recommendation(compact):
        return False
    direct_cues = (
        "我主要",
        "我想",
        "我就想",
        "我想要",
        "我希望",
        "我需要",
        "我觉得我",
        "我觉得自己",
        "我的",
        "我这个",
        "像我这种",
    )
    if not any(cue in compact for cue in direct_cues):
        return False
    if not keywords:
        return True
    return _text_contains_any_keyword(compact, keywords)


def _looks_like_customer_answer_after_elicitation(segment: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    text = _clean_text(segment.get("text"))
    if not text or not _looks_like_direct_customer_primary_demand_line(text):
        return False
    if _looks_like_staff_explanatory_statement(text) or _looks_like_staff_product_explanation_or_self_example(text):
        return False
    try:
        index = next(candidate for candidate, item in enumerate(segments) if item is segment)
    except StopIteration:
        return False
    speaker_id = _clean_text(segment.get("speaker_id"))
    for previous in segments[max(0, index - 4) : index]:
        previous_text = _clean_text(previous.get("text"))
        if not previous_text:
            continue
        if speaker_id and _clean_text(previous.get("speaker_id")) == speaker_id:
            continue
        if _looks_like_customer_elicitation_question(previous_text) and any(
            cue in previous_text for cue in ("你", "您", "自己", "主要", "解决", "做过", "有没有")
        ):
            return True
    return False


def _is_mislabeled_customer_candidate(segment: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    """Detect customer-like turns mislabeled as staff_peer/员工同事.

    Tencent diarization can label the non-badge speaker as staff_peer. Keep this
    narrow: only use it when there are virtually no explicit customer turns and
    the text itself contains customer-side payment, travel, decision, or treatment
    arrangement signals.
    """
    role = _normalized_segment_role(segment)
    business_role = _normalized_segment_business_role(segment)
    label = _normalized_segment_label(segment)
    if "工牌本人" in {role, business_role, label}:
        return False
    is_doctor_role = bool({role, business_role, label} & {"医生", "doctor"})
    if not ({role, business_role, label} & _MISLABELED_CUSTOMER_CANDIDATE_ROLES) and not is_doctor_role:
        return False
    text = _clean_text(segment.get("text"))
    if not text:
        return False
    if any(cue in text for cue in _MISLABELED_CUSTOMER_INTERNAL_CUES):
        return False
    if _looks_like_customer_answer_after_elicitation(segment, segments):
        return True
    if _customer_segment_ratio(segments) >= _LOW_PARTICIPATION_THRESHOLD:
        return False
    if is_doctor_role:
        return _looks_like_doctor_role_customer_speech(text)
    return any(cue in text for cue in _MISLABELED_CUSTOMER_SELF_OR_DECISION_CUES)


def _looks_like_customer_speech_mislabeled_as_badge_owner(text: str) -> bool:
    """Detect customer-side utterances that were mislabeled as badge_owner."""
    normalized = _clean_text(text)
    if not normalized:
        return False
    if any(cue in normalized for cue in _BADGE_OWNER_STAFF_SPEECH_CUES):
        return False
    return any(cue in normalized for cue in _MISLABELED_BADGE_OWNER_CUSTOMER_SPEECH_CUES)


def _segment_has_staff_signature(segment: dict[str, Any]) -> bool:
    return (
        _normalized_segment_business_role(segment) in _STAFF_SIDE_ROLES
        or _normalized_segment_label(segment) in _STAFF_SIDE_ROLES
    )


def _iter_following_customer_segments(
    segments: list[dict[str, Any]],
    index: int,
    *,
    lookahead: int = 4,
    limit: int = 2,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for offset in range(1, lookahead + 1):
        if index + offset >= len(segments):
            break
        segment = segments[index + offset]
        if not _is_customer_side_segment(segment):
            continue
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        collected.append(segment)
        if len(collected) >= limit:
            break
    return collected


def _text_matches_any_pattern(text: str, patterns: tuple[Any, ...]) -> bool:
    compact = re.sub(r"\s+", "", text)
    for pattern in patterns:
        if hasattr(pattern, "search"):
            if pattern.search(text):
                return True
            continue
        if re.search(str(pattern), compact):
            return True
    return False


def _text_contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def _text_contains_excluded_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def _is_brief_customer_confirmation(text: str) -> bool:
    normalized = _clean_text(text).strip("，。！？,.!~～")
    if not normalized:
        return False
    if normalized in _CUSTOMER_CONFIRMATION_HINTS:
        return True
    cleaned = normalized.strip("嗯啊呀哈吧呢啦")
    if not cleaned:
        return True
    if cleaned in _CUSTOMER_CONFIRMATION_HINTS:
        return True
    if len(cleaned) <= 8 and any(pattern.match(cleaned) for pattern in _CUSTOMER_CONFIRMATION_PATTERNS):
        return True
    return False


def _staff_segment_looks_customer_specific(text: str) -> bool:
    return any(cue in text for cue in _STAFF_CONFIRMATION_CUES)


def _looks_like_customer_self_report_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if not any(cue in normalized for cue in _CUSTOMER_SELF_REPORT_HINTS):
        return False
    return any(cue in normalized for cue in _CUSTOMER_SELF_REPORT_FACT_HINTS)


def _looks_like_customer_elicitation_question(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if any(cue in normalized for cue in _CUSTOMER_ELICITATION_HINTS):
        return True
    if normalized.endswith(("吗", "呢", "呀", "吧", "？", "?")):
        return True
    return False


def _split_statement_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for raw_text in _normalize_text_list(text):
        normalized = _clean_text(raw_text)
        if not normalized:
            continue
        for clause in re.split(r"[，,。；;！？!?\n]+", normalized):
            cleaned = clause.strip()
            if cleaned:
                clauses.append(cleaned)
    return clauses


def _looks_like_customer_self_fact_clause(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    self_patterns = (
        r"我(?:想|就想|想要|自己|本人|现在|以前|之前|主要|本身|觉得|需要|要|这个|这种|就是|先|不|没|没有)",
        r"我的",
        r"我有(?!个)",
        r"对我",
        r"给我",
        r"适合我",
        r"帮我",
        r"推荐我",
        r"陪我",
        r"像我(?:这个|这种)",
        r"我[^，。；;]{0,6}(?:皮肤|鼻子|眼袋|泪沟|毛孔|痘痘|脸|眼睛|鼻背|鼻翼|山根|肤质|法令纹|颈纹|疤痕|双眼皮)",
    )
    return any(re.search(pattern, compact) for pattern in self_patterns)


def _looks_like_safe_relation_context_clause(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(
        pattern in compact
        for pattern in (
            "推荐我",
            "介绍我",
            "带我来",
            "陪我来",
            "陪我来的",
            "跟我来",
            "和我来",
            "陪我一起",
            "跟我一起",
            "和我一起",
            "帮我约",
            "跟我商量",
            "和我商量",
            "给我意见",
            "支持我",
            "陪我看",
            "陪我咨询",
        )
    ):
        return True
    family_relations = ("老公", "丈夫", "老婆", "妻子", "男朋友", "女朋友", "对象", "恋人", "妈妈", "母亲", "爸爸", "父母")
    if any(keyword in compact for keyword in family_relations) and any(
        action in compact for action in _FAMILY_DECISION_ACTION_HINTS + _FAMILY_DECISION_ACTION_STRONG_HINTS
    ):
        return True
    return False


def _looks_like_third_party_experience_clause(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if _looks_like_safe_relation_context_clause(compact):
        return False
    if _looks_like_customer_self_fact_clause(compact):
        return False
    has_relation = any(
        hint in compact for hint in _THIRD_PARTY_FACT_RELATION_HINTS + _THIRD_PARTY_FACT_REPORTING_HINTS
    ) or bool(
        re.search(
            r"(?:他|她)(?:是)?(?:当时|之前|后来|今天|说|做|打|来|有|没|没有|想|这样|这种|这个|那种|的|脸上|皮肤|身上|鼻子|眼睛|眼袋|泪沟|嘴|唇|毛孔|痘痘|疤痕|法令纹)",
            compact,
        )
    )
    if not has_relation:
        return False
    has_experience = any(hint in compact for hint in _THIRD_PARTY_FACT_EXPERIENCE_HINTS) or bool(
        re.search(
            r"(?:他|她).{0,16}(?:做|打|说|想|有|没|没有|术后|恢复|不满意|害怕|纠结|痘痘|毛孔|眼袋|泪沟|鼻|皮肤|肤质|法令纹|纹路|唇|嘴)",
            compact,
        )
    )
    return has_experience


def _clause_has_third_party_context(clauses: list[str], index: int) -> bool:
    if index < 0 or index >= len(clauses):
        return False
    clause = clauses[index]
    if _looks_like_third_party_experience_clause(clause):
        return True
    if _looks_like_customer_self_fact_clause(clause) or _looks_like_safe_relation_context_clause(clause):
        return False
    for prev_index in range(index - 1, max(-1, index - 3), -1):
        previous_clause = clauses[prev_index]
        if _looks_like_customer_self_fact_clause(previous_clause):
            break
        if _looks_like_third_party_experience_clause(previous_clause):
            return True
    return False


def _keywords_only_appear_in_third_party_clauses(text: str, keywords: tuple[str, ...]) -> bool:
    clauses = _split_statement_clauses(text)
    if not clauses:
        return False
    matched_indexes = [
        index
        for index, clause in enumerate(clauses)
        if any(keyword and keyword in clause for keyword in keywords)
    ]
    if not matched_indexes:
        return False
    if any(_looks_like_customer_self_fact_clause(clauses[index]) for index in matched_indexes):
        return False
    return all(_clause_has_third_party_context(clauses, index) for index in matched_indexes)


def _looks_like_third_party_narrative_statement(text: str, *, keywords: tuple[str, ...] = ()) -> bool:
    if keywords:
        return _keywords_only_appear_in_third_party_clauses(text, keywords)
    clauses = _split_statement_clauses(text)
    if not clauses:
        return False
    if any(_looks_like_customer_self_fact_clause(clause) for clause in clauses):
        return False
    return any(_clause_has_third_party_context(clauses, index) for index in range(len(clauses)))


def _supported_mislabeled_customer_self_report_source(
    segments: list[dict[str, Any]],
    index: int,
) -> tuple[str, str] | None:
    if index < 0 or index >= len(segments):
        return None
    segment = segments[index]
    if not _is_staff_side_segment(segment):
        return None
    text = _clean_text(segment.get("text"))
    if not _looks_like_customer_self_report_statement(text):
        return None
    business_role = _normalized_segment_business_role(segment)
    if business_role not in {"工牌本人", "badge_owner"}:
        return None
    for offset in (-2, -1, 1, 2):
        peer_index = index + offset
        if peer_index < 0 or peer_index >= len(segments):
            continue
        peer_segment = segments[peer_index]
        if not _is_customer_side_segment(peer_segment):
            continue
        if not _segment_has_staff_signature(peer_segment):
            continue
        peer_text = _clean_text(peer_segment.get("text"))
        if _looks_like_customer_elicitation_question(peer_text):
            return text, _segment_evidence(segment)
    return None


def _supported_mislabeled_customer_brief_answer_source(
    segments: list[dict[str, Any]],
    index: int,
) -> tuple[str, str] | None:
    if index < 0 or index >= len(segments):
        return None
    segment = segments[index]
    if not _is_staff_side_segment(segment):
        return None
    if _normalized_segment_business_role(segment) not in {"工牌本人", "badge_owner"}:
        return None
    text = _clean_text(segment.get("text"))
    if not text or len(text) > 24:
        return None
    if _looks_like_customer_elicitation_question(text) or _looks_like_staff_explanatory_statement(text):
        return None
    if not any(cue in text for cue in ("三年前", "年前", "满意", "不满意", "没有", "后悔", "喜欢", "想", "第一次", "做过")):
        return None
    for offset in (-2, -1):
        peer_index = index + offset
        if peer_index < 0:
            continue
        peer_segment = segments[peer_index]
        if not _is_customer_side_segment(peer_segment) or not _segment_has_staff_signature(peer_segment):
            continue
        peer_text = _clean_text(peer_segment.get("text"))
        if not _looks_like_customer_elicitation_question(peer_text):
            continue
        combined_text = "\n".join(part for part in (peer_text, text) if part)
        combined_evidence = "\n".join(
            part for part in (_segment_evidence(peer_segment), _segment_evidence(segment)) if part
        )
        return combined_text, combined_evidence
    return None


def _supported_mislabeled_customer_history_cluster_source(
    segments: list[dict[str, Any]],
    index: int,
) -> tuple[str, str] | None:
    if index < 0 or index >= len(segments):
        return None
    segment = segments[index]
    if not _is_staff_side_segment(segment):
        return None
    if _normalized_segment_business_role(segment) not in {"工牌本人", "badge_owner"}:
        return None
    text = _clean_text(segment.get("text"))
    if not text or len(text) > 32:
        return None
    if not any(cue in text for cue in ("三年前", "年前", "满意", "不满意", "后悔", "第一次", "做过")):
        return None
    window_indexes = [candidate for candidate in range(max(0, index - 5), index + 1)]
    window_segments = [segments[candidate] for candidate in window_indexes]
    window_text = "\n".join(_clean_text(item.get("text")) for item in window_segments if _clean_text(item.get("text")))
    if not any(keyword in window_text for _, keywords in _TREATMENT_HISTORY_HINTS for keyword in keywords):
        return None
    if not any(
        _is_customer_side_segment(item)
        and _segment_has_staff_signature(item)
        and _looks_like_customer_elicitation_question(_clean_text(item.get("text")))
        for item in window_segments
    ):
        return None
    return window_text, "\n".join(_segment_evidence(item) for item in window_segments if _segment_evidence(item))


def _looks_like_injection_history_question(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    return any(hint in compact for hint in _INJECTION_HISTORY_QUESTION_HINTS)


def _looks_like_injection_history_answer(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if any(hint in compact for hint in _INJECTION_HISTORY_EXCLUDED_HINTS):
        return False
    if _looks_like_customer_elicitation_question(normalized):
        return False
    return bool(_INJECTION_HISTORY_ANSWER_RE.search(compact))


def _is_badge_owner_segment(segment: dict[str, Any]) -> bool:
    return "工牌本人" in {
        _normalized_segment_role(segment),
        _normalized_segment_business_role(segment),
        _normalized_segment_label(segment),
    }


def _history_context_text(
    segments: list[dict[str, Any]],
    question_index: int,
    answer_index: int,
    *,
    lookback: int = 18,
) -> str:
    start = max(0, question_index - lookback)
    stop = min(len(segments), answer_index + 1)
    return "\n".join(
        _clean_text(segments[candidate].get("text"))
        for candidate in range(start, stop)
        if _clean_text(segments[candidate].get("text"))
    )


def _has_history_context(text: str, hints: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    return any(hint in compact for hint in hints)


def _supported_history_device_source(
    segments: list[dict[str, Any]],
    index: int,
    *,
    keyword: str,
) -> tuple[str, str] | None:
    if index < 0 or index >= len(segments):
        return None
    segment = segments[index]
    text = _clean_text(segment.get("text"))
    if not text or keyword not in text:
        return None
    if not _looks_like_profile_tag_statement("历史用的设备/原材料名称", keyword, text):
        return None
    if _is_customer_side_segment(segment) or _is_mislabeled_customer_candidate(segment, segments):
        return text, _segment_evidence(segment)
    if not _is_staff_side_segment(segment):
        return None
    compact = re.sub(r"\s+", "", text)
    keyword_compact = re.sub(r"\s+", "", keyword)
    if not re.search(
        rf"(?:你|您).{{0,32}}(?:以前|之前|原来|当时|上次|那次|做过|打过|做了|打了|打的|注射过|填过).{{0,32}}{re.escape(keyword_compact)}",
        compact,
    ):
        return None
    evidence_parts = [_segment_evidence(segment)]
    for following in _iter_following_customer_segments(segments, index):
        following_text = _clean_text(following.get("text"))
        if not following_text or _looks_like_customer_elicitation_question(following_text):
            continue
        evidence_parts.append(_segment_evidence(following))
        break
    return text, "\n".join(part for part in evidence_parts if part)


def _infer_ambiguous_hit_history_cluster(
    segments: list[dict[str, Any]],
    *,
    context_hints: tuple[str, ...],
    excluded_context_hints: tuple[str, ...] = (),
) -> str | None:
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not _looks_like_injection_history_answer(text):
            continue
        answer_compact = re.sub(r"\s+", "", text)
        if context_hints == _ENERGY_HISTORY_CONTEXT_HINTS and re.search(r"(?:打过|打了)", answer_compact):
            continue
        is_customer_answer = _is_customer_side_segment(segment)
        is_badge_owner_relay = _is_staff_side_segment(segment) and _is_badge_owner_segment(segment)
        if not is_customer_answer and not is_badge_owner_relay:
            continue

        window_indexes = range(max(0, index - 3), index)
        for question_index in window_indexes:
            question_segment = segments[question_index]
            question_text = _clean_text(question_segment.get("text"))
            if not _looks_like_injection_history_question(question_text):
                continue
            if not (_is_customer_side_segment(question_segment) or _is_staff_side_segment(question_segment)):
                continue
            context_text = _history_context_text(segments, question_index, index)
            if not _has_history_context(context_text, context_hints):
                continue
            local_context = "\n".join(part for part in (question_text, text) if part)
            if (
                excluded_context_hints
                and _has_history_context(context_text, excluded_context_hints)
                and not _has_history_context(local_context, context_hints)
            ):
                continue
            evidence = "\n".join(
                part
                for part in (_segment_evidence(question_segment), _segment_evidence(segment))
                if part
            )
            if evidence:
                return evidence
    return None


def _infer_injection_history_cluster_evidence(segments: list[dict[str, Any]]) -> str | None:
    return _infer_ambiguous_hit_history_cluster(
        segments,
        context_hints=_INJECTION_HISTORY_CONTEXT_HINTS,
        excluded_context_hints=_ENERGY_HISTORY_CONTEXT_HINTS,
    )


def _infer_energy_history_cluster_evidence(segments: list[dict[str, Any]]) -> str | None:
    return _infer_ambiguous_hit_history_cluster(
        segments,
        context_hints=_ENERGY_HISTORY_CONTEXT_HINTS,
        excluded_context_hints=_INJECTION_HISTORY_CONTEXT_HINTS,
    )


def _infer_surgical_history_cluster_evidence(segments: list[dict[str, Any]]) -> str | None:
    for index, segment in enumerate(segments):
        if not _is_staff_side_segment(segment):
            continue
        if _normalized_segment_business_role(segment) not in {"工牌本人", "badge_owner"}:
            continue
        text = _clean_text(segment.get("text"))
        if not text or not any(cue in text for cue in ("三年前", "年前", "后悔", "不满意", "做过")):
            continue
        window_indexes = [candidate for candidate in range(max(0, index - 5), index + 1)]
        window_segments = [segments[candidate] for candidate in window_indexes]
        window_text = "\n".join(_clean_text(item.get("text")) for item in window_segments if _clean_text(item.get("text")))
        compact_window = re.sub(r"\s+", "", window_text)
        if any(cue in compact_window for cue in ("他们本身", "其他地方", "别人", "人家", "案例", "好多人", "我们给他们")):
            continue
        if not any(keyword in compact_window for keyword in ("双眼皮", "眼袋手术", "鼻综合", "隆鼻", "提眉", "手术", "假体", "膨体", "做过鼻子", "动过鼻子")):
            continue
        if not any(
            _is_customer_side_segment(item)
            and _segment_has_staff_signature(item)
            and _looks_like_customer_elicitation_question(_clean_text(item.get("text")))
            for item in window_segments
        ):
            continue
        return "\n".join(_segment_evidence(item) for item in window_segments if _segment_evidence(item))
    return None


def _infer_negative_project_tag(segments: list[dict[str, Any]]) -> tuple[str, str] | None:
    future_or_worry_markers = ("万一", "担心", "怕", "如果", "后期", "怎么办", "会不会")
    past_history_markers = (
        "以前",
        "之前",
        "原来",
        "上次",
        "那次",
        "几年前",
        "年前",
        "做过",
        "打过",
        "做了",
        "打了",
        "术后",
        "当时",
    )

    def _score_candidate(window_text: str, keywords: tuple[str, ...]) -> int:
        compact_window = re.sub(r"\s+", "", _clean_text(window_text))
        if not compact_window:
            return -1
        best_score = -1
        for keyword in keywords:
            compact_keyword = re.sub(r"\s+", "", keyword)
            if not compact_keyword:
                continue
            start = compact_window.find(compact_keyword)
            while start >= 0:
                end = start + len(compact_keyword)
                nearby_window = compact_window[max(0, start - 24) : min(len(compact_window), end + 72)]
                after_window = compact_window[end : min(len(compact_window), end + 72)]
                score = 0
                if any(marker in nearby_window for marker in past_history_markers):
                    score += 3
                if any(marker in after_window for marker in _NEGATIVE_PROJECT_CUES + _NEGATIVE_PROJECT_REPAIR_CUES):
                    score += 3
                if any(marker in nearby_window for marker in ("尝试", "打了一点", "补过", "填过")):
                    score += 1
                score += len(compact_keyword)
                best_score = max(best_score, score)
                start = compact_window.find(compact_keyword, end)
        return best_score

    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text or not any(cue in text for cue in _NEGATIVE_PROJECT_CUES):
            continue
        compact_text = re.sub(r"\s+", "", text)
        if any(marker in compact_text for marker in future_or_worry_markers) and not any(
            marker in compact_text for marker in past_history_markers
        ):
            continue
        if not (_is_customer_side_segment(segment) or _is_mislabeled_customer_candidate(segment, segments) or _is_staff_side_segment(segment)):
            continue
        window_indexes = [candidate for candidate in range(max(0, index - 16), index + 1)]
        window_segments = [segments[candidate] for candidate in window_indexes]
        window_text = "\n".join(_clean_text(item.get("text")) for item in window_segments if _clean_text(item.get("text")))
        candidate_matches: list[tuple[str, tuple[str, ...], int]] = []
        for candidate_value, keywords in _NEGATIVE_PROJECT_VALUE_HINTS:
            if not any(keyword in window_text for keyword in keywords):
                continue
            if not _keyword_has_treatment_history_context(window_text, keywords):
                continue
            candidate_matches.append((candidate_value, keywords, _score_candidate(window_text, keywords)))
        if not candidate_matches:
            continue
        value, value_keywords, _ = max(candidate_matches, key=lambda item: item[2])
        value_segments = [
            item
            for item in window_segments
            if _segment_evidence(item)
            and (
                any(keyword in _clean_text(item.get("text")) for _, keywords in _NEGATIVE_PROJECT_VALUE_HINTS for keyword in keywords)
            )
        ]
        negative_segments = [
            item
            for item in window_segments
            if _segment_evidence(item)
            and (
                any(cue in _clean_text(item.get("text")) for cue in _NEGATIVE_PROJECT_CUES)
                or "满意吗" in _clean_text(item.get("text"))
            )
        ]
        evidence_segments = [*(value_segments[:1]), *(negative_segments[-2:])]
        evidence = "\n".join(dict.fromkeys(_segment_evidence(item) for item in evidence_segments if _segment_evidence(item)))
        if evidence:
            return value, evidence
    return None


def _infer_eyelid_shape_demand_evidence(segments: list[dict[str, Any]]) -> str | None:
    for index, segment in enumerate(segments):
        if not _is_staff_side_segment(segment):
            continue
        if _normalized_segment_business_role(segment) not in {"工牌本人", "badge_owner"}:
            continue
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if not any(cue in text for cue in ("形象不太满意", "后悔", "没有以前", "细长一点好看", "双眼皮")):
            continue
        window_indexes = [candidate for candidate in range(max(0, index - 3), min(len(segments), index + 2))]
        window_segments = [segments[candidate] for candidate in window_indexes]
        window_text = "\n".join(_clean_text(item.get("text")) for item in window_segments if _clean_text(item.get("text")))
        if "双眼皮" not in window_text:
            continue
        if not any(
            _is_customer_side_segment(item)
            and _segment_has_staff_signature(item)
            and (
                _looks_like_customer_elicitation_question(_clean_text(item.get("text")))
                or "诉求" in _clean_text(item.get("text"))
            )
            for item in window_segments
        ):
            continue
        return "\n".join(_segment_evidence(item) for item in window_segments if _segment_evidence(item))
    return None


def _confirmed_staff_segment_evidence(
    segments: list[dict[str, Any]],
    index: int,
    *,
    keywords: tuple[str, ...] = (),
    patterns: tuple[Any, ...] = (),
    allow_money: bool = False,
) -> str | None:
    if index < 0 or index >= len(segments):
        return None
    staff_segment = segments[index]
    if not _is_staff_side_segment(staff_segment):
        return None
    staff_text = _clean_text(staff_segment.get("text"))
    if not staff_text:
        return None
    staff_specific = _staff_segment_looks_customer_specific(staff_text)
    customer_segments = _iter_following_customer_segments(segments, index)
    for customer_segment in customer_segments:
        customer_text = _clean_text(customer_segment.get("text"))
        if not customer_text:
            continue
        if _looks_like_mislabeled_staff_customer_segment(customer_segment):
            continue
        if _looks_like_customer_elicitation_question(customer_text):
            continue
        if (keywords and _keywords_only_appear_in_third_party_clauses(customer_text, keywords)) or (
            not keywords and _looks_like_third_party_narrative_statement(customer_text)
        ):
            continue
        explicit_match = (
            _text_contains_any_keyword(customer_text, keywords)
            or _text_matches_any_pattern(customer_text, patterns)
            or (allow_money and _extract_money_text(customer_text) is not None)
        )
        if explicit_match or (staff_specific and _is_brief_customer_confirmation(customer_text)):
            return "\n".join(
                evidence
                for evidence in (
                    _segment_evidence(staff_segment),
                    _segment_evidence(customer_segment),
                )
                if evidence
            )
    return None


def _supported_fact_source(
    segments: list[dict[str, Any]],
    index: int,
    *,
    keywords: tuple[str, ...] = (),
    patterns: tuple[Any, ...] = (),
    allow_money: bool = False,
    excluded_keywords: tuple[str, ...] = (),
    allow_weak_staff_inference: bool = False,
) -> tuple[str, str] | None:
    segment = segments[index]
    text = _clean_text(segment.get("text"))
    if not text:
        return None
    if excluded_keywords and _text_contains_excluded_keyword(text, excluded_keywords):
        return None
    if _looks_like_staff_product_explanation_or_self_example(text):
        return None
    if _looks_like_mislabeled_staff_customer_segment(segment):
        return None
    if _is_customer_side_segment(segment):
        if _segment_has_staff_signature(segment) and _looks_like_customer_elicitation_question(text):
            return None
        if (keywords and _keywords_only_appear_in_third_party_clauses(text, keywords)) or (
            not keywords and _looks_like_third_party_narrative_statement(text)
        ):
            return None
        return text, _segment_evidence(segment)
    if _is_mislabeled_customer_candidate(segment, segments):
        if (
            (keywords and _text_contains_any_keyword(text, keywords))
            or (patterns and _text_matches_any_pattern(text, patterns))
            or (allow_money and _extract_money_text(text) is not None)
            or any(cue in text for cue in _MISLABELED_CUSTOMER_SELF_OR_DECISION_CUES)
        ):
            if (keywords and _keywords_only_appear_in_third_party_clauses(text, keywords)) or (
                not keywords and _looks_like_third_party_narrative_statement(text)
            ):
                return None
            return text, _segment_evidence(segment)
    if _is_staff_side_segment(segment):
        if _is_badge_owner_segment(segment) and _looks_like_customer_speech_mislabeled_as_badge_owner(text):
            return text, _segment_evidence(segment)
        self_report = _supported_mislabeled_customer_self_report_source(segments, index)
        if self_report is not None:
            return self_report
        brief_answer = _supported_mislabeled_customer_brief_answer_source(segments, index)
        if brief_answer is not None:
            return brief_answer
        history_cluster = _supported_mislabeled_customer_history_cluster_source(segments, index)
        if history_cluster is not None:
            return history_cluster
        evidence = _confirmed_staff_segment_evidence(
            segments,
            index,
            keywords=keywords,
            patterns=patterns,
            allow_money=allow_money,
        )
        if evidence:
            return text, evidence
        if (
            allow_weak_staff_inference
            and
            _customer_segment_ratio(segments) < _LOW_PARTICIPATION_THRESHOLD
            and len(text) >= 6
        ):
            if (keywords and _text_contains_any_keyword(text, keywords)) or (
                patterns and _text_matches_any_pattern(text, patterns)
            ):
                return text, _segment_evidence(segment)
    return None


def _keywords_for_primary_demand(demand: str) -> tuple[str, ...]:
    normalized = _clean_text(demand)
    for hint_demand, _body_part, keywords in _PRIMARY_DEMAND_SEED_HINTS + _PRIMARY_DEMAND_ISSUE_HINTS:
        if normalized == hint_demand or hint_demand in normalized or normalized in hint_demand:
            return keywords
    concept_keywords: list[str] = []
    for _concept, keywords in _PRIMARY_DEMAND_CONCEPT_HINTS:
        if any(keyword in normalized for keyword in keywords):
            concept_keywords.extend(keywords)
    if concept_keywords:
        deduped: list[str] = []
        for keyword in concept_keywords:
            if keyword and keyword not in deduped:
                deduped.append(keyword)
        return tuple(deduped)
    compact = re.sub(r"[；，。、“”‘’（）()：: ]+", "", normalized)
    return (compact,) if compact else ()


def _excluded_keywords_for_primary_demand(demand: str) -> tuple[str, ...]:
    return _PRIMARY_DEMAND_EXCLUDED_KEYWORDS.get(_clean_text(demand), ())


def _allows_weak_main_fact_fallback(segments: list[dict[str, Any]]) -> bool:
    return bool(segments) and _customer_segment_ratio(segments) < _LOW_PARTICIPATION_THRESHOLD


def _has_concrete_main_fact_context(
    segments: list[dict[str, Any]],
    *,
    staff_recommendations_payload: dict[str, Any] | None = None,
) -> bool:
    """Whether the consultation has a concrete project/part/plan thread.

    This is intentionally narrower than general weak evidence. It only opens the
    floor for the two core fields (primary demand and indication), so a real
    consultation with clear plan discussion does not end up with 0 items.
    """
    if isinstance(staff_recommendations_payload, dict) and _as_list(staff_recommendations_payload.get("items")):
        return True
    if not segments:
        return False

    project_keywords: list[str] = []
    for _plan_name, _body_part, keywords in _PLAN_HINTS:
        project_keywords.extend(keywords)
    for hint in _INDICATION_HINTS:
        project_keywords.extend(tuple(hint.get("keywords", ())))
    plan_cues = _PLAN_RECOMMENDATION_CUES + ("报价", "价格", "费用", "多少钱", "开单", "下单", "付款", "定金", "核销", "排期", "安排")

    start_index = _find_consultation_start_index(segments)
    for segment in segments[start_index:]:
        text = _clean_text(segment.get("text"))
        if len(text) < 8:
            continue
        if any(keyword in text for keyword in project_keywords) and any(cue in text for cue in plan_cues):
            return True
    return False


def _allows_main_fact_floor(
    segments: list[dict[str, Any]],
    *,
    staff_recommendations_payload: dict[str, Any] | None = None,
) -> bool:
    """Open the 1-demand/1-indication floor only for sparse medical scenes.

    Normal-volume consultations should optimize for precision: if a main fact
    lacks strong/medium evidence, leave it empty rather than backfilling from
    weak staff-only context.
    """
    if not segments or not _is_sparse_effective_consultation(segments):
        return False

    start_index = _find_consultation_start_index(segments)
    full_text = " ".join(_clean_text(segment.get("text")) for segment in segments[start_index:])
    if not full_text:
        return False
    compact_text = re.sub(r"\s+", "", full_text)
    if any(cue in compact_text for cue in _SPARSE_NON_BUSINESS_NEGATION_CUES):
        return False

    has_medical_keyword = _text_contains_any_keyword(full_text, _sparse_medical_business_keywords())
    if not has_medical_keyword:
        return False
    return _text_contains_any_keyword(full_text, _SPARSE_MEDICAL_BUSINESS_INTENT_CUES) or _has_concrete_main_fact_context(
        segments,
        staff_recommendations_payload=staff_recommendations_payload,
    )


def _indication_hint_for_name(indication_name: str) -> dict[str, Any] | None:
    normalized = _clean_text(indication_name)
    for hint in _INDICATION_HINTS:
        if _clean_text(hint.get("indication_name")) == normalized:
            return hint
    return None


def _find_supported_evidence_for_keywords(
    segments: list[dict[str, Any]],
    *,
    keywords: tuple[str, ...] = (),
    patterns: tuple[Any, ...] = (),
    excluded_keywords: tuple[str, ...] = (),
    allow_money: bool = False,
    allow_weak_staff_inference: bool = False,
) -> str | None:
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if keywords and not _text_contains_any_keyword(text, keywords):
            continue
        supported = _supported_fact_source(
            segments,
            index,
            keywords=keywords,
            patterns=patterns,
            excluded_keywords=excluded_keywords,
            allow_money=allow_money,
            allow_weak_staff_inference=allow_weak_staff_inference,
        )
        if supported is not None:
            return supported[1]
    return None


def _find_supported_evidence_from_existing_text(
    segments: list[dict[str, Any]],
    *,
    existing_evidence: str,
    keywords: tuple[str, ...] = (),
    patterns: tuple[Any, ...] = (),
    excluded_keywords: tuple[str, ...] = (),
    allow_money: bool = False,
    allow_weak_staff_inference: bool = False,
) -> str | None:
    for raw_text in _normalize_text_list(existing_evidence):
        for raw_line in raw_text.splitlines():
            line = re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
            if not line:
                continue
            for index, segment in enumerate(segments):
                text = _clean_text(segment.get("text"))
                if not text or line not in text:
                    continue
                supported = _supported_fact_source(
                    segments,
                    index,
                    keywords=keywords or (line,),
                    patterns=patterns,
                    excluded_keywords=excluded_keywords,
                    allow_money=allow_money,
                    allow_weak_staff_inference=allow_weak_staff_inference,
                )
                if supported is not None:
                    # Prefer the transcript-backed evidence reconstructed by
                    # _supported_fact_source. It may include the neighboring
                    # question/confirmation needed to make the conclusion
                    # readable, while the persisted model excerpt is often a
                    # shortened sentence that no longer proves the field.
                    return supported[1]
    return None


_DIRECT_PROFILE_TAG_CUES: dict[tuple[str, str], tuple[str, ...]] = {
    ("创伤倾向", "手术"): ("我想通过手术", "通过手术", "想做手术", "考虑做手术", "接受手术", "做手术"),
    ("创伤倾向", "微创"): ("不想开刀", "不想手术", "怕手术", "通过微创", "想做微创", "考虑微创", "微创"),
    ("创伤倾向", "皮肤"): ("做皮肤", "皮肤管理", "光电", "做光电", "只做皮肤"),
    ("倾向回访方式", "微信"): _WECHAT_FOLLOW_UP_HINTS,
    ("倾向回访方式", "电话"): ("电话联系", "电话回访", "打电话", "打给"),
    ("倾向回访方式", "短信"): ("短信联系", "短信回访", "发短信"),
}


def _find_direct_customer_profile_tag_evidence(
    segments: list[dict[str, Any]],
    *,
    category: str,
    value: str,
) -> str | None:
    cues = _DIRECT_PROFILE_TAG_CUES.get((category, value))
    if not cues:
        return None
    for segment in segments:
        text = _clean_text(segment.get("text"))
        if not text or not any(cue in text for cue in cues):
            continue
        if not (
            _is_customer_side_segment(segment)
            or _is_mislabeled_customer_candidate(segment, segments)
            or (_is_badge_owner_segment(segment) and _looks_like_customer_speech_mislabeled_as_badge_owner(text))
        ):
            continue
        evidence = _segment_evidence(segment)
        if evidence and _looks_like_profile_tag_statement(category, value, evidence):
            return evidence
    return None


def _find_supported_profile_tag_evidence(
    segments: list[dict[str, Any]],
    *,
    category: str,
    value: str,
    existing_evidence: str,
    keywords: tuple[str, ...] = (),
    patterns: tuple[Any, ...] = (),
) -> str | None:
    direct_candidate = _find_direct_customer_profile_tag_evidence(
        segments,
        category=category,
        value=value,
    )
    if direct_candidate:
        return direct_candidate

    existing_candidate = _find_supported_evidence_from_existing_text(
        segments,
        existing_evidence=existing_evidence,
        keywords=keywords,
        patterns=patterns,
    )
    if existing_candidate and _looks_like_profile_tag_statement(category, value, existing_candidate):
        return existing_candidate

    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if keywords and not _text_contains_any_keyword(text, keywords):
            continue
        if patterns and not _text_matches_any_pattern(text, patterns):
            continue
        supported = _supported_fact_source(
            segments,
            index,
            keywords=keywords,
            patterns=patterns,
        )
        if supported is None:
            continue
        evidence = supported[1]
        if _looks_like_profile_tag_statement(category, value, evidence):
            return evidence
    return None


def _looks_like_treatment_history_statement(text: str) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if _looks_like_future_or_hypothetical_treatment_reference(normalized):
        return False
    if _looks_like_material_brand_explanation_without_customer_history(normalized):
        return False
    if any(
        cue in compact
        for cue in (
            "我以为你取了假体",
            "以为你取了假体",
            "像假体",
            "像做了假体",
            "是不是打的",
            "是不是做的",
            "像打过",
            "像做过",
        )
    ):
        return False
    if any(hint in compact for hint in ("如果", "假如", "假设", "要是", "看你如果")) and any(
        hint in compact for hint in ("没做过", "没有做过", "未做过", "从来没做过", "第一次")
    ):
        return False
    no_prior_removed = re.sub(r"(?:没|没有|未|从来没)做过|第一次", "", compact)
    if any(hint in compact for hint in ("没做过", "没有做过", "未做过", "从来没做过", "第一次")) and not any(
        hint in no_prior_removed for hint in ("做过", "打过", "做了", "打了", "术后")
    ):
        return False
    has_self_report = any(cue in normalized for cue in _CUSTOMER_SELF_REPORT_HINTS)
    explicit_history_action = any(
        cue in normalized
        for cue in ("做过", "打过", "做了", "打了", "打的", "注射过", "填过", "术后", "没做过", "没有做过", "第一次")
    )
    if has_self_report and any(
        cue in normalized for cue in ("以前", "之前", "原来", "做过", "打过", "做了", "打了", "术后", "恢复")
    ):
        return True
    if any(cue in normalized for cue in ("建议", "推荐", "适合", "可以做", "先做", "先建议", "想不想", "要不要", "要打", "打针的话", "做的话", "还不如做", "配合")):
        return False
    if "如果" in normalized and "没做过" in normalized:
        return False
    if explicit_history_action:
        return True
    return has_self_report and any(cue in normalized for cue in ("以前", "之前", "原来", "恢复", "曾经"))


def _looks_like_future_or_hypothetical_treatment_reference(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    past_markers = (
        "以前",
        "之前",
        "原来",
        "上次",
        "那次",
        "几年前",
        "年前",
        "个月前",
        "做过",
        "打过",
        "术后",
    )
    if any(marker in compact for marker in past_markers):
        return False
    return bool(
        re.search(r"(?:如果|要是|万一).{0,16}(?:做|打|注射|填充)", compact)
        or re.search(r"(?:做了|打了|注射了|填充了).{0,16}(?:以后|之后|会|容易|可能|后期|移位|馒化)", compact)
        or re.search(r"(?:做完|打完|注射完|填充完).{0,16}(?:以后|之后|会|容易|可能|后期|移位|馒化)", compact)
    )


def _looks_like_material_brand_explanation_without_customer_history(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    brand_or_material = ("玻尿酸", "斐然", "润致", "乔雅登", "海薇", "艾莉薇", "伊婉", "濡白天使", "贝丽菲尔", "贝利菲尔", "菲利菲尔", "Bellafill")
    explanation_cues = (
        "品牌",
        "材料",
        "价格",
        "毫升量",
        "一支",
        "适应症",
        "专门批",
        "批复",
        "华西生物",
        "医药公司",
        "自己人打的最多",
    )
    if not any(keyword in compact for keyword in brand_or_material):
        return False
    if not any(cue in compact for cue in explanation_cues):
        return False
    strong_customer_history = (
        "我以前打过",
        "我之前打过",
        "我原来打过",
        "我上次打过",
        "我打过这个",
        "我做过这个",
        "给我打过",
        "给我做过",
    )
    return not any(cue in compact for cue in strong_customer_history)


def _keyword_has_treatment_history_context(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False
    if _looks_like_future_or_hypothetical_treatment_reference(normalized):
        return False
    if _looks_like_material_brand_explanation_without_customer_history(normalized):
        return False
    history_actions = (
        "做过",
        "打过",
        "做了",
        "打了",
        "打的",
        "注射过",
        "填过",
        "术后",
        "来做过",
        "以前做",
        "之前做",
        "原来做",
    )
    negative_history_actions = ("没做过", "没有做过", "未做过", "从来没做过", "第一次")
    generic_future_or_recommendation = (
        "建议",
        "推荐",
        "适合",
        "可以做",
        "要做的话",
        "做的话",
        "想不想",
        "要不要",
        "想做",
        "想了解",
        "咨询",
        "考虑做",
        "打算做",
        "准备做",
    )
    future_plan_explanation = (
        "不会一来就让你做",
        "能用微创",
        "能做无创",
        "无创解决不了",
        "微创改善不了",
        "让你做手术",
    )
    effect_or_future_context = (
        "手术痕迹",
        "看不出来手术",
        "看不出来那个手术",
        "恢复期",
        "恢复多久",
        "变化更大",
        "效果",
        "方案",
    )
    past_context_markers = ("以前", "之前", "原来", "当时", "头几年", "上次", "那次", "几年前", "年前", "术后")
    staffish_explanation = _looks_like_staff_explanatory_statement(normalized) or _looks_like_staff_demo_or_example_statement(normalized)
    third_party_markers = ("同事", "朋友", "别人", "人家", "案例", "顾客", "他做", "她做", "他打", "她打", "男的", "女的")

    if any(cue in compact for cue in future_plan_explanation):
        return False
    if any(cue in compact for cue in ("如果", "假如", "假设", "要是", "看你如果")) and not any(
        cue in compact for cue in history_actions + past_context_markers
    ):
        return False
    if any(cue in compact for cue in effect_or_future_context) and not any(
        cue in compact for cue in history_actions + past_context_markers
    ):
        return False
    if any(cue in compact for cue in generic_future_or_recommendation) and not any(
        cue in compact for cue in history_actions + past_context_markers
    ):
        return False

    for keyword in keywords:
        compact_keyword = re.sub(r"\s+", "", keyword)
        if not compact_keyword:
            continue
        marker_index = compact.find(compact_keyword)
        has_third_party_context = False
        while marker_index >= 0:
            marker_end = marker_index + len(compact_keyword)
            marker_window = compact[max(0, marker_index - 24) : min(len(compact), marker_end + 12)]
            if any(marker in marker_window for marker in third_party_markers):
                has_third_party_context = True
                break
            marker_index = compact.find(compact_keyword, marker_end)
        if has_third_party_context:
            continue
        explicit_keyword_history_patterns = (
            rf"{re.escape(compact_keyword)}.{{0,16}}(?:做过|打过|做了|打了|打的|注射过|填过|术后|不满意|后悔)",
            rf"(?:做过|打过|做了|打了|打的|注射过|填过|术后).{{0,16}}{re.escape(compact_keyword)}",
        )
        if any(re.search(pattern, compact) for pattern in explicit_keyword_history_patterns):
            return True
        strong_self_report_patterns = (
            rf"(?:我|本人|自己|以前|之前|原来|当时|头几年|上次|那次).{{0,16}}(?:做过|打过|做了|打了|打的|注射过|填过|来做过).{{0,12}}{re.escape(compact_keyword)}",
            rf"(?:我|本人|自己|以前|之前|原来|当时|头几年|上次|那次).{{0,16}}{re.escape(compact_keyword)}.{{0,12}}(?:做过|打过|做了|打了|打的|注射过|填过|术后)",
            rf"(?:以前|之前|原来|头几年|上次|那次|几年前|年前).{{0,16}}{re.escape(compact_keyword)}.{{0,10}}(?:手术|术后|不满意|后悔)",
        )
        staff_safe_self_report_patterns = (
            rf"(?:我|本人|自己|以前|之前|原来|当时|头几年|上次|那次).{{0,16}}(?:做过|打过|打的|注射过|填过|来做过).{{0,12}}{re.escape(compact_keyword)}",
            rf"(?:我|本人|自己|以前|之前|原来|当时|头几年|上次|那次).{{0,16}}{re.escape(compact_keyword)}.{{0,12}}(?:做过|打过|做了|打了|打的|注射过|填过|术后)",
            rf"(?:以前|之前|原来|头几年|上次|那次|几年前|年前).{{0,16}}{re.escape(compact_keyword)}.{{0,10}}(?:手术|术后|不满意|后悔)",
        )
        if any(re.search(pattern, compact) for pattern in strong_self_report_patterns):
            if not staffish_explanation or any(re.search(pattern, compact) for pattern in staff_safe_self_report_patterns):
                return True
        staff_recap_patterns = (
            rf"(?:你|您).{{0,16}}(?:已经|以前|之前|原来|当时|三年前|几年前)?(?:做过|打过|做了|打了|打的|注射过|填过).{{0,14}}{re.escape(compact_keyword)}",
            rf"(?:你|您).{{0,16}}{re.escape(compact_keyword)}.{{0,14}}(?:做过|做了|手术|术后|不满意|后悔)",
        )
        if any(re.search(pattern, compact) for pattern in staff_recap_patterns):
            return True
        if staffish_explanation:
            continue
        start = compact.find(compact_keyword)
        while start >= 0:
            end = start + len(compact_keyword)
            window = compact[max(0, start - 8) : min(len(compact), end + 8)]
            before_window = compact[max(0, start - 10) : end]
            after_window = compact[start : min(len(compact), end + 10)]
            if any(action in window for action in negative_history_actions):
                start = compact.find(compact_keyword, end)
                continue
            if any(action in before_window for action in history_actions):
                return True
            if any(marker in after_window for marker in ("手术", "术后", "不满意", "后悔")) and any(
                cue in compact for cue in ("以前", "之前", "原来", "头几年", "做过", "打过", "做了", "打了")
            ):
                return True
            start = compact.find(compact_keyword, end)

    if any(cue in compact for cue in generic_future_or_recommendation):
        return False
    return False


def _looks_like_no_prior_treatment_statement(text: str) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False

    lines: list[str] = []
    for raw_text in _normalize_text_list(text):
        for raw_line in raw_text.splitlines():
            line = re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
            if line:
                lines.append(line)

    if not lines:
        lines = [normalized]

    def _looks_like_no_prior_question_clause(line_compact: str, start: int, end: int) -> bool:
        question_window = line_compact[max(0, start - 6) : min(len(line_compact), end + 8)]
        suffix = line_compact[end: min(len(line_compact), end + 6)]
        if start > 0 and line_compact[start - 1] == "有":
            return True
        if any(
            hint in question_window
            for hint in (
                "有没有做过",
                "有没做过",
                "做过没有",
                "做过没",
                "没做过吗",
                "没做过吧",
                "是不是第一次",
                "是第一次吗",
                "第一次吗",
                "第一次做吗",
            )
        ):
            return True
        return any(hint in suffix or hint in question_window for hint in ("对吧", "是吧"))

    def _looks_like_positive_treatment_history_line(line: str) -> bool:
        line_compact = re.sub(r"\s+", "", _clean_text(line))
        if not line_compact:
            return False
        if any(
            hint in line_compact
            for hint in (
                "有没有做过",
                "有没做过",
                "做过没有",
                "做过没",
                "做过吗",
                "打过没有",
                "打过没",
                "打过吗",
                "有没有打过",
            )
        ):
            return False
        if any(
            hint in line_compact
            for hint in ("没做过", "没有做过", "未做过", "从来没做过", "第一次", "没打过", "没有打过")
        ):
            return False
        return bool(
            re.search(
                r"(?:我|本人|自己|嗯|对|是|以前|之前|原来|上次|那次|几年前|[一二两三四五六七八九十\d]+年前).{0,10}"
                r"(?:做过|打过|做了|打了|来做过)(?:一次|一针|几次)?",
                line_compact,
            )
            or re.search(r"(?:做过|打过)(?:一次|一针|几次|了)", line_compact)
            or "术后" in line_compact
        )

    confirmation_present = any(
        _is_brief_customer_confirmation(line)
        or (
            len(re.sub(r"\s+", "", _clean_text(line))) <= 12
            and any(token in re.sub(r"\s+", "", _clean_text(line)) for token in ("是第一次", "就是第一次", "没做过", "没有做过"))
        )
        for line in lines
    )
    positive_history_present = any(_looks_like_positive_treatment_history_line(line) for line in lines)
    staffish_statement = _looks_like_staff_explanatory_statement(normalized) or _looks_like_staff_demo_or_example_statement(normalized)

    for line in lines:
        line_compact = re.sub(r"\s+", "", _clean_text(line))
        if not line_compact:
            continue
        matches = [
            match
            for pattern in _NO_PRIOR_TREATMENT_TAG_PATTERNS
            for match in re.finditer(pattern, line_compact)
        ]
        if not matches:
            continue
        for match in matches:
            start, end = match.span()
            prefix = line_compact[max(0, start - 12) : start]
            window = line_compact[max(0, start - 18) : min(len(line_compact), end + 18)]
            has_self_context = prefix.endswith(("我", "本人", "自己"))
            has_second_person_context = prefix.endswith(("你", "您"))
            has_reporting_context = any(hint in prefix or hint in window for hint in _NO_PRIOR_TREATMENT_REPORTING_HINTS)
            has_third_party_context = any(hint in prefix or hint in window for hint in _NO_PRIOR_TREATMENT_THIRD_PARTY_HINTS) or prefix.endswith(("他", "她"))
            hypothetical_context = any(hint in prefix or hint in window for hint in ("如果", "假如", "假设", "要是", "看你"))
            question_clause = _looks_like_no_prior_question_clause(line_compact, start, end)

            if hypothetical_context and not has_self_context:
                continue
            if has_reporting_context:
                continue
            if has_third_party_context and not has_self_context:
                continue
            if positive_history_present:
                continue
            if question_clause and not confirmation_present:
                continue
            if has_second_person_context and not confirmation_present:
                continue
            if staffish_statement and not has_self_context and not confirmation_present:
                continue
            return True
    return False


def _looks_like_local_city_statement(text: str, value: str) -> bool:
    normalized = _clean_text(text)
    if value == "本地":
        if any(cue in normalized for cue in ("不像本地", "不是本地", "不算本地")):
            return False
        return any(cue in normalized for cue in _LOCAL_CITY_SELF_REPORT_CUES)
    if value == "外地":
        if any(cue in normalized for cue in ("像外地人", "不像本地人")) and not any(
            cue in normalized for cue in ("我是外地", "我不是本地", "从外地", "外地来")
        ):
            return False
        return any(cue in normalized for cue in _NON_LOCAL_CITY_SELF_REPORT_CUES)
    return False


def _looks_like_no_negative_project_statement(text: str) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False
    if any(keyword in compact for keyword in ("无创", "无痛", "无痕", "无菌", "无针", "无框")):
        return False
    if any(keyword in compact for keyword in _NEGATIVE_PROJECT_CUES):
        return False
    return bool(
        re.search(r"(?:没有|没|无).{0,8}(?:踩雷|翻车|失败|不满意|负面|过敏|后悔|排斥|项目|设备|材料|原材料)", compact)
        or re.search(r"(?:踩雷|翻车|失败|不满意|负面|过敏|后悔|排斥|项目|设备|材料|原材料).{0,8}(?:没有|没|无)", compact)
    )


def _normalize_age_value(value: Any) -> str | None:
    normalized = _clean_text(value)
    match = _AGE_VALUE_RE.search(normalized)
    if not match:
        return None
    age = int(match.group(1))
    if 10 <= age <= 100:
        return f"{age}{match.group(2)}"
    return None


def _age_context_window(text: str, start: int, end: int, *, radius: int = 24) -> str:
    return text[max(0, start - radius): min(len(text), end + radius)]


def _age_mention_is_example_or_negation(text: str, match: re.Match[str]) -> bool:
    age_text = f"{int(match.group(1))}{match.group(2)}"
    start, end = match.span()
    before = text[max(0, start - 8): start]
    after = text[end: min(len(text), end + 16)]
    window = _age_context_window(text, start, end)
    compact_window = re.sub(r"\s+", "", window)
    compact_prefix = re.sub(r"\s+", "", text[max(0, start - 16): start])
    compact_suffix = re.sub(r"\s+", "", text[end: min(len(text), end + 24)])
    compact_age = f"{int(match.group(1))}{match.group(2)}"

    if _looks_like_staff_self_treatment_or_age_example(window):
        return True
    if any(hint in window for hint in ("案例", "顾客", "别人", "人家", "朋友", "同事", "比如", "比如说")):
        return True
    if re.search(r"比(?:你|您|她|他).{0,4}(?:大|小)" + re.escape(age_text), window):
        return True
    if any(pattern in f"{before}{age_text}" for pattern in (f"不像{age_text}", f"不是{age_text}", f"不到{age_text}")):
        return True
    if any(pattern in f"{before}{match.group(1)}" for pattern in (f"不像{match.group(1)}", f"不是{match.group(1)}", f"不到{match.group(1)}")):
        return True
    if compact_prefix.endswith("从") and re.match(
        r"(?:到|开始|起|那年|那会|那时候|时|读大学|上大学|大学|毕业|工作|上班|从业|入行|进入|接触)",
        compact_suffix,
    ):
        return True
    if re.search(re.escape(compact_age) + r"(?:到|开始|起|那年|那会|那时候)", compact_window) and any(
        hint in compact_window for hint in ("大学", "毕业", "工作", "上班", "从业", "入行", "进入", "行业", "接触")
    ):
        return True
    if re.search(r"(?:到|到了|等到|变到|再到)\s*$", before):
        return True
    if "多岁" in after[:3] or "来岁" in after[:3]:
        return True
    if any(hint in window for hint in _AGE_EXAMPLE_OR_NEGATION_HINTS) and not _AGE_QUESTION_HINT_RE.search(window):
        return True
    return False


def _extract_supported_birthdate(text: str) -> str | None:
    normalized = _clean_text(text)
    if not normalized:
        return None

    # Exact birthday/year values are reliable only when the sentence is about
    # birthday/birth year. This avoids treating project years or event dates as
    # profile values.
    for pattern in _BIRTHDATE_PATTERNS:
        match = pattern.search(normalized)
        if match and any(cue in normalized for cue in ("出生", "生日", "几几年的", "哪一年")):
            return match.group(1)


def _extract_supported_age(text: str) -> str | None:
    normalized = _clean_text(text)
    if not normalized:
        return None

    candidates: list[tuple[int, int, str]] = []
    for match in _AGE_VALUE_RE.finditer(normalized):
        if _age_mention_is_example_or_negation(normalized, match):
            continue
        age = int(match.group(1))
        if not (10 <= age <= 100):
            continue
        start, end = match.span()
        window = _age_context_window(normalized, start, end)
        age_text = f"{age}{match.group(2)}"
        score = 0
        if _AGE_QUESTION_HINT_RE.search(window):
            score += 8
        if re.search(r"(?:我|你|您|她|他)(?:今年|现在)?[^，。；;]{0,12}" + re.escape(age_text), window):
            score += 6
        if re.search(r"(?:我|你|您|她|他)[^，。；;]{0,12}(?:年龄|多大|几岁)", window):
            score += 4
        if re.search(r"(?:今年多大|年龄)[^，。；;]{0,8}\d{2,3}\s*" + re.escape(age_text), window):
            score += 4
        if score <= 0:
            continue
        candidates.append((score, -start, age_text))

    for match in _AGE_VALUE_WITHOUT_SUFFIX_RE.finditer(normalized):
        age = int(match.group(1))
        if not (10 <= age <= 100):
            continue
        start, end = match.span(1)
        window = _age_context_window(normalized, start, end)
        if _looks_like_staff_self_treatment_or_age_example(window):
            continue
        if any(hint in window for hint in _AGE_EXAMPLE_OR_NEGATION_HINTS) and "身份证号年龄" not in window and not _AGE_QUESTION_HINT_RE.search(window):
            continue
        score = 0
        if "身份证号年龄" in window:
            score += 12
        if _AGE_QUESTION_HINT_RE.search(window):
            score += 8
        if re.search(r"(?:我|你|您|她|他)(?:今年|现在)?[^，。；;]{0,12}" + re.escape(str(age)), window):
            score += 4
        if score <= 0:
            continue
        candidates.append((score, -start, f"{age}岁"))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _extract_age_from_customer_answer(text: str) -> str | None:
    normalized = _clean_text(text)
    if not normalized:
        return None

    match = _AGE_VALUE_RE.search(normalized)
    if match and not _age_mention_is_example_or_negation(normalized, match):
        age = int(match.group(1))
        if 10 <= age <= 100:
            return f"{age}{match.group(2)}"

    match = re.search(r"(?:还没有|还没|没|没有)?(?:满|到)?\s*(\d{2})\s*(?:岁)?", normalized)
    if match and any(cue in normalized for cue in ("满", "岁", "年龄", "多大", "几岁")):
        age = int(match.group(1))
        if "满" in normalized and any(cue in normalized for cue in ("还没有", "还没", "没", "没有")):
            age -= 1
        if 10 <= age <= 100:
            return f"{age}岁"
    return None


def _find_adjacent_age_answer(segments: list[dict[str, Any]]) -> tuple[str, str] | None:
    for index, segment in enumerate(segments):
        question_text = _clean_text(segment.get("text"))
        if not question_text or not _AGE_QUESTION_HINT_RE.search(question_text):
            continue
        if _is_customer_side_segment(segment):
            continue

        evidence_parts: list[str] = []
        question_evidence = _segment_evidence(segment)
        if question_evidence:
            evidence_parts.append(question_evidence)

        question_end = int(segment.get("end") or segment.get("end_ms") or 0)
        for next_segment in segments[index + 1 : index + 5]:
            next_text = _clean_text(next_segment.get("text"))
            if not next_text:
                continue
            next_begin = int(next_segment.get("begin") or next_segment.get("begin_ms") or 0)
            if question_end and next_begin and next_begin - question_end > 12_000:
                break

            age = _extract_age_from_customer_answer(next_text)
            if age and (_is_customer_side_segment(next_segment) or not _is_staff_side_segment(next_segment)):
                next_evidence = _segment_evidence(next_segment)
                if next_evidence:
                    evidence_parts.append(next_evidence)
                # Include immediate staff confirmation such as "相当于21岁".
                for confirm_segment in segments[index + 2 : index + 6]:
                    confirm_text = _clean_text(confirm_segment.get("text"))
                    confirm_age = _extract_age_from_customer_answer(confirm_text)
                    if confirm_age == age:
                        confirm_evidence = _segment_evidence(confirm_segment)
                        if confirm_evidence:
                            evidence_parts.append(confirm_evidence)
                        break
                return age, "\n".join(evidence_parts[-2:] if len(evidence_parts) > 2 else evidence_parts)
    return None


def _find_supported_age_evidence(segments: list[dict[str, Any]]) -> tuple[str, str] | None:
    adjacent = _find_adjacent_age_answer(segments)
    if adjacent is not None:
        return adjacent

    candidates: list[tuple[int, int, str, str]] = []
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        age = _extract_supported_age(text)
        if not age:
            continue
        evidence: str | None = None
        score = 0
        if "身份证号年龄" in text:
            evidence = _segment_evidence(segment)
            score += 20
        else:
            supported = _supported_fact_source(
                segments,
                index,
                keywords=("年龄", "多大", "几岁", "多少岁"),
                patterns=(_AGE_VALUE_RE, _AGE_VALUE_WITHOUT_SUFFIX_RE),
            )
            if supported is not None:
                evidence = supported[1]
                score += 10
        if not evidence:
            continue
        if _is_staff_side_segment(segment):
            score -= 8
        if _is_customer_side_segment(segment):
            score += 3
        if index <= 20:
            score += 4
        candidates.append((score, -index, age, evidence))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    _score, _index, age, evidence = candidates[0]
    return age, evidence


def _looks_like_birthdate_statement(text: str, value: str) -> bool:
    normalized = _clean_text(text)
    normalized_value = _clean_text(value)
    if not normalized_value:
        return False

    if _normalize_age_value(normalized_value):
        return False

    supported_value = _extract_supported_birthdate(normalized)
    if supported_value:
        return supported_value.replace(" ", "") == normalized_value.replace(" ", "")

    if not any(pattern.search(normalized) for pattern in _BIRTHDATE_PATTERNS):
        return False
    if normalized_value not in normalized:
        return False
    return any(cue in normalized for cue in ("出生", "生日", "几几年的", "哪一年"))


def _looks_like_personal_status_statement(text: str, value: str) -> bool:
    normalized = _clean_text(text)
    if value == "已婚":
        return any(keyword in normalized for keyword in ("我老公", "我丈夫", "我老婆", "我妻子", "已婚"))
    if value == "有恋人":
        return any(keyword in normalized for keyword in ("我男朋友", "我女朋友", "我对象", "我恋人", "有对象"))
    if value == "单身":
        return "单身" in normalized
    return False


def _looks_like_children_statement(text: str) -> bool:
    normalized = _clean_text(text)
    return any(keyword in normalized for keyword in ("无孩", "没孩子", "未育", "一孩", "一个孩子", "一个娃", "二孩", "两个孩子", "2孩", "2个孩子", "三孩"))


def _looks_like_followup_channel_statement(text: str, value: str) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if any(cue in compact for cue in ("给我发微信", "不停的给我发微信", "他给我发微信", "她给我发微信")):
        return False
    if value == "微信":
        return "微信" in normalized and any(hint in normalized for hint in _WECHAT_FOLLOW_UP_HINTS)
    if value == "电话":
        return "电话" in normalized and any(hint in normalized for hint in ("联系", "回访", "打给", "电话沟通"))
    if value == "短信":
        return "短信" in normalized and any(hint in normalized for hint in ("联系", "回访", "发短信"))
    return False


def _looks_like_staff_extra_problem_or_recommendation(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(
        cue in compact
        for cue in (
            "我主要",
            "我想",
            "我就想",
            "我想要",
            "我希望",
            "我需要",
            "我觉得我",
            "我觉得自己",
        )
    ) and not any(cue in compact for cue in ("我觉得你", "我建议你", "我给你", "我帮你")):
        return False
    staff_specific_phrases = (
        "后期我觉得你",
        "你还有一点",
        "你有点点",
        "你有一点",
        "你后期",
        "你要去做",
        "早点控制",
        "以后形成真性皱纹",
        "形成真性皱纹",
        "只有填胶原",
        "最主要是做",
        "我反而会推荐你",
        "我不推荐你",
        "我建议你",
        "可以考虑",
        "适合你",
        "适合不适合",
        "你皮肤状态很好",
        "像有些人",
        "很多亚洲人",
        "后期可以",
        "就后期可以",
    )
    if any(phrase in compact for phrase in staff_specific_phrases):
        return True
    if any(term in compact for term in ("口周抗衰", "唇周", "白唇", "人中", "干纹", "细纹", "真性皱纹")) and any(
        cue in compact for cue in ("后期", "最主要", "可以做", "建议", "推荐", "给你", "你还有", "你后期", "形成")
    ):
        return True
    if re.search(r"(?:后期|后面).{0,8}(?:可以|填一下|做|重视|改善)", compact):
        return True
    if re.search(r"(?:你|您).{0,6}(?:有点|有一点|有点点).{0,14}(?:眼袋|泪沟|松弛|下垂|口周|唇周|细纹|皱纹|法令纹|凹陷|黑眼圈)", compact):
        return True
    return bool(re.search(r"(?:你|您).{0,10}(?:后期|可以|建议|适合|推荐|要去做|重视|主要是做|形成)", compact))


def _looks_like_staff_explanatory_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if _looks_like_staff_extra_problem_or_recommendation(normalized):
        return True
    if normalized.startswith("你") and any(keyword in normalized for keyword in ("只能", "可以", "建议", "适合", "打针", "手术", "提眉", "双眼皮", "改善")):
        return True
    if any(
        phrase in normalized
        for phrase in (
            "以前的眼睛",
            "现在眼睛",
            "三角眼",
            "整体是因为",
            "往下走",
            "颧骨",
            "骨头",
            "空间不大",
            "松弛的皮肤",
        )
    ):
        return True
    second_person_hits = sum(keyword in normalized for keyword in ("你", "你这", "你要", "你的", "你现在", "您", "咱们"))
    explanatory_hits = sum(keyword in normalized for keyword in _STAFF_EXPLANATORY_HINTS)
    return second_person_hits >= 2 and explanatory_hits >= 2


def _looks_like_staff_demo_or_example_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if "你看" in compact and _looks_like_direct_customer_primary_demand_line(compact):
        return False
    return any(keyword in normalized for keyword in _STAFF_DEMO_HINTS)


def _looks_like_staff_self_treatment_or_age_example(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False
    if any(cue in compact for cue in _STAFF_SELF_TREATMENT_OR_AGE_EXAMPLE_CUES):
        return True
    if re.search(r"(?:我|本人)(?:不?是|也不是)?(?:00后|零零后)", compact) and any(
        cue in compact for cue in ("全脸", "自然", "看不出来", "别人看不出来", "做了")
    ):
        return True
    if re.search(r"(?:我|本人).{0,8}\d{2,3}(?:岁|多岁)", compact) and any(
        cue in compact for cue in ("全脸", "自然", "看不出来", "别人看不出来", "做了", "不是00后", "不是零零后")
    ):
        return True
    if re.search(r"(?:我是|我也|我自己|我本人|像我).{0,12}(?:全脸|抗衰|医美).{0,20}(?:做了|做完|极致|自然|看不出来)", compact):
        return True
    if re.search(r"(?:我脸上|我鼻子|我鼻基底).{0,16}(?:一万|四五万|[一二三四五六七八九十\d]+万|花了)", compact):
        return True
    return False


def _looks_like_staff_product_explanation_or_self_example(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False
    if _looks_like_staff_self_treatment_or_age_example(compact):
        return True
    if sum(1 for cue in _STAFF_SCHEDULING_CUES if cue in compact) >= 2:
        return True
    if not any(term in compact for term in _PRODUCT_EXPLANATION_TERMS):
        return False
    if any(cue in compact for cue in _STAFF_SELF_EXAMPLE_CUES):
        return True
    if any(cue in compact for cue in _STAFF_PRODUCT_EXPLANATION_CUES) and any(
        cue in compact
        for cue in ("可以改善", "长期打", "一年打", "几支", "一支", "含量", "提取", "商城", "秒杀", "套餐")
    ):
        return True
    return False


def _looks_like_mislabeled_staff_customer_segment(segment: dict[str, Any]) -> bool:
    if not _is_customer_side_segment(segment):
        return False
    business_role = _normalized_segment_business_role(segment)
    label = _normalized_segment_label(segment)
    if business_role not in _STAFF_SIDE_ROLES and label not in _STAFF_SIDE_ROLES:
        return False
    text = _clean_text(segment.get("text"))
    if not text:
        return False
    if any(
        token in text
        for token in (
            "我的",
            "我想",
            "我怕",
            "我上班",
            "我以前",
            "我之前",
            "我做过",
            "我没做过",
            "我就是",
            "我觉得",
            "我主要",
            "我不想",
            "我没有",
            "我有",
            "我现在",
        )
    ):
        return False
    if any(keyword in text for keyword in _STAFF_TECHNICAL_HINTS):
        return True
    explanatory_hits = sum(keyword in text for keyword in _STAFF_EXPLANATORY_HINTS)
    if explanatory_hits >= 2:
        return True
    if _looks_like_staff_demo_or_example_statement(text):
        return True
    return _looks_like_staff_explanatory_statement(text)


def _has_health_topic_context(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(keyword in compact for keyword in _NON_HEALTH_NEGATION_CONTEXT_HINTS):
        return False
    return any(keyword in compact for keyword in _HEALTH_TOPIC_HINTS)


def _looks_like_negative_health_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if not _has_health_topic_context(normalized):
        return False
    lines = _evidence_text_lines(normalized) or [normalized]
    for line in lines:
        compact = re.sub(r"\s+", "", _clean_text(line))
        if not compact:
            continue
        if any(keyword in compact for keyword in _NON_HEALTH_NEGATION_CONTEXT_HINTS):
            continue
        if "有没有" in compact and not any(keyword in compact for keyword in ("没有没有", "都没有", "没有的", "没有哈", "没有哦")):
            continue
        if any(pattern.search(compact) for pattern in _NEGATIVE_HEALTH_PATTERNS):
            return True
    return False


def _looks_like_positive_health_statement(text: str, value: str) -> bool:
    normalized = _clean_text(text)
    if _looks_like_negative_health_statement(normalized):
        return False
    if any(keyword in normalized for keyword in _HEALTH_QUESTION_HINTS) or any(
        keyword in normalized for keyword in ("要查", "查你", "查一下", "筛查", "检查")
    ):
        return any(keyword in normalized for keyword in _POSITIVE_HEALTH_SELF_REPORT_HINTS)
    return True


def _looks_like_decision_maker_statement(value: str, text: str) -> bool:
    normalized = _clean_text(text)
    if value == "父母":
        return any(keyword in normalized for keyword in ("妈妈", "母亲", "爸爸", "父母")) and any(
            action in normalized for action in _FAMILY_DECISION_ACTION_STRONG_HINTS
        )
    if value == "伴侣":
        return any(keyword in normalized for keyword in ("老公", "丈夫", "老婆", "妻子", "男朋友", "女朋友", "对象", "恋人")) and any(
            action in normalized for action in _FAMILY_DECISION_ACTION_STRONG_HINTS
        )
    if value == "自主":
        return any(keyword in normalized for keyword in ("自己决定", "我自己定", "我自己做主", "我自己说了算"))
    return False


def _looks_like_price_sensitivity_statement(value: str, text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if value == "高":
        strong_patterns = (
            r"(?:太贵|有点贵|价格高|预算有限|烧钱|便宜点|能不能便宜)",
            r"(?:有没有|有无|还有|可不可以|能不能).{0,8}(?:活动|优惠|补贴|折扣)",
            r"(?:活动|优惠|补贴|折扣).{0,8}(?:有没有|有无|还有|可不可以|能不能)",
            r"(?:一分钱|少不了|少一点|少点|便宜些|打折)",
            r"(?:怎么又多|多了|太多).{0,8}(?:钱|块|元|费用|价格|预算)",
            r"(?:钱|块|元|费用|价格|预算).{0,8}(?:怎么又多|多了|太多)",
        )
        return any(re.search(pattern, compact) for pattern in strong_patterns)
    if value == "中":
        return any(keyword in compact for keyword in ("价格中等", "中等一点", "价格合适", "划算"))
    if value == "低":
        return any(keyword in compact for keyword in ("价格不是问题", "不考虑价格", "预算充足"))
    return False


def _looks_like_special_identity_statement(value: str, text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if value == "黑名单":
        return any(keyword in compact for keyword in ("黑名单", "被拉黑", "拉黑名单"))
    if value == "竞对同行":
        if re.search(r"(?:不是|不算|没有|非).{0,4}(?:同行|竞对|竞品)", compact):
            return False
        strong_self_report = (
            "我是同行",
            "我也是同行",
            "我们也是做医美",
            "我是做医美",
            "我在医美",
            "在医美上班",
            "在美容院上班",
            "在整形医院上班",
        )
        industry_context = ("同行机构", "竞对机构", "竞品机构", "竞对", "竞品")
        return any(keyword in compact for keyword in strong_self_report + industry_context)
    return False


def _looks_like_profile_tag_statement(category: str, value: str, text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if value and _looks_like_third_party_narrative_statement(normalized, keywords=(value,)):
        return False
    if _looks_like_third_party_narrative_statement(normalized):
        return False

    if category == "治疗项目":
        if value == "无医美史":
            return _looks_like_no_prior_treatment_statement(normalized)
        for candidate, keywords in _TREATMENT_HISTORY_HINTS:
            if value == candidate:
                if _keywords_only_appear_in_third_party_clauses(normalized, keywords):
                    return False
                return any(keyword in normalized for keyword in keywords) and _keyword_has_treatment_history_context(normalized, keywords)
        if _keywords_only_appear_in_third_party_clauses(normalized, (value,)):
            return False
        return value in normalized and _keyword_has_treatment_history_context(normalized, (value,))

    if category == "历史用的设备/原材料名称":
        if _keywords_only_appear_in_third_party_clauses(normalized, (value,)):
            return False
        return value in normalized and _looks_like_treatment_history_statement(normalized)

    if category == "健康风险/禁忌":
        if value == "无风险禁忌":
            return _looks_like_negative_health_statement(normalized)
        for candidate, keywords in _HEALTH_TAG_HINTS:
            if value == candidate:
                if _keywords_only_appear_in_third_party_clauses(normalized, keywords):
                    return False
                return any(keyword in normalized for keyword in keywords) and _looks_like_positive_health_statement(normalized, value)
        return False

    if category == "负面项目/设备/原材料":
        if value == "无":
            return False
        if _keywords_only_appear_in_third_party_clauses(normalized, (value,)):
            return False
        return value in normalized and _looks_like_treatment_history_statement(normalized)

    if category in {"出生日期", "出生日期/年龄"}:
        return _looks_like_birthdate_statement(normalized, value)

    if category == "常驻城市":
        return _looks_like_local_city_statement(normalized, value)

    if category == "价格敏感度":
        for candidate, keywords in _PRICE_SENSITIVITY_HINTS:
            if value == candidate:
                return any(keyword in normalized for keyword in keywords) and _looks_like_price_sensitivity_statement(value, normalized)
        return False

    if category == "特殊身份":
        for candidate, keywords in _SPECIAL_IDENTITY_HINTS:
            if value == candidate:
                return any(keyword in normalized for keyword in keywords) and _looks_like_special_identity_statement(value, normalized)
        return False

    if category == "个人情况":
        return _looks_like_personal_status_statement(normalized, value)

    if category == "决策主体":
        return _looks_like_decision_maker_statement(value, normalized)

    if category == "亲属/子女情况":
        return _looks_like_children_statement(normalized)

    if category == "倾向回访方式":
        return _looks_like_followup_channel_statement(normalized, value)

    return True


def _profile_tag_evidence_supports_value(category: str, value: str, evidence: str) -> bool:
    if _looks_like_profile_tag_statement(category, value, evidence):
        return True
    aliases: list[str] = []
    if category == "历史用的设备/原材料名称":
        aliases.extend(
            keyword
            for keyword in _HISTORY_DEVICE_HINTS
            if canonicalize_profile_tag_value(category, keyword) == value
        )
    for alias in aliases:
        if alias != value and alias in evidence and _looks_like_profile_tag_statement(category, alias, evidence):
            return True
    return False


def _evidence_text_lines(evidence: str) -> list[str]:
    lines: list[str] = []
    for raw_text in _normalize_text_list(evidence):
        for raw_line in raw_text.splitlines():
            cleaned = re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
            if cleaned:
                lines.append(cleaned)
    return lines


def _looks_like_indication_evidence(
    indication_name: str,
    evidence: str,
    *,
    allow_weak_staff_inference: bool = False,
) -> bool:
    normalized_name = _clean_text(indication_name)
    lines = _evidence_text_lines(evidence)
    if not normalized_name or not lines:
        return False

    customer_line = _clean_text(lines[-1])
    if not customer_line:
        return False
    hint = _indication_hint_for_name(normalized_name)
    indication_keywords = tuple(hint.get("keywords", ())) if hint else (normalized_name,)
    if indication_keywords and not _text_contains_any_keyword("\n".join(lines), indication_keywords):
        return False
    if len(lines) >= 2 and _is_brief_customer_confirmation(customer_line):
        previous_line = _clean_text(lines[-2])
        if _looks_like_staff_explanatory_statement(previous_line) and not _text_contains_any_keyword(
            previous_line,
            indication_keywords,
        ):
            return False
    if _looks_like_third_party_narrative_statement(customer_line, keywords=indication_keywords):
        return False
    if _looks_like_prior_treatment_only_statement(customer_line, indication_keywords):
        return False
    if _looks_like_staff_self_treatment_or_age_example(customer_line) and not any(
        _looks_like_direct_customer_primary_demand_line(line, indication_keywords) for line in lines
    ):
        return False
    if _looks_like_staff_product_explanation_or_self_example(customer_line):
        return False

    if normalized_name == "疤痕" and any(keyword in customer_line for keyword in _SCAR_CONSTITUTION_HINTS):
        return False
    if normalized_name == "眼袋":
        if _looks_like_negated_eye_bag_or_tear_trough(customer_line):
            return False
        if _looks_like_eye_injection_plastic_context(customer_line) and not any(
            keyword in customer_line for keyword in ("眼袋", "眶隔", "内切", "外切", "祛眼袋", "去眼袋")
        ):
            return False
    if normalized_name == "纹路" and _looks_like_wrinkle_tolerance_without_treatment_intent(customer_line):
        return False
    if normalized_name == "面部除皱" and _looks_like_prior_botulinum_outcome_statement(customer_line):
        return False
    if normalized_name == "面部填充":
        if _looks_like_filler_indication_statement(customer_line):
            return True
        if allow_weak_staff_inference:
            compact = re.sub(r"\s+", "", _clean_text(customer_line))
            filler_terms = ("面部填充", "填充", "玻尿酸", "苹果肌", "法令纹", "太阳穴", "鼻基底", "下巴", "凹陷")
            plan_cues = ("可以", "建议", "适合", "通过", "方案", "让", "改善", "调整")
            return any(term in compact for term in filler_terms) and any(cue in compact for cue in plan_cues)
        return False
    if normalized_name == "敏感":
        return _looks_like_sensitive_indication_statement(customer_line)
    if normalized_name == "鼻综合":
        return _looks_like_rhinoplasty_indication_statement(customer_line)
    if normalized_name == "双眼皮":
        return _looks_like_eyelid_shape_indication_statement(customer_line)
    if _looks_like_staff_demo_or_example_statement(customer_line) and not _looks_like_direct_customer_primary_demand_line(
        customer_line,
        indication_keywords,
    ):
        return False
    if (
        _looks_like_staff_explanatory_statement(customer_line)
        and not allow_weak_staff_inference
        and not _looks_like_direct_customer_primary_demand_line(customer_line, indication_keywords)
    ):
        return False

    if normalized_name == "干燥":
        return _looks_like_dryness_indication_statement(customer_line)

    if normalized_name in _INDICATION_SELF_REPORT_REQUIRED:
        if normalized_name in {"双眼皮", "提眉"}:
            has_action_intent = any(keyword in customer_line for keyword in ("想做", "想要", "做", "修复", "改善"))
            if "喜欢" in customer_line and not has_action_intent:
                return False
        has_intent = any(keyword in customer_line for keyword in _INDICATION_INTENT_HINTS)
        has_question = any(keyword in customer_line for keyword in _INDICATION_QUESTION_HINTS)
        has_self_report = any(keyword in customer_line for keyword in _INDICATION_SELF_REPORT_HINTS)
        if not has_intent and not has_self_report:
            return False
        if not has_intent and has_question:
            return False

    return True


def _looks_like_prior_botulinum_outcome_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if not any(keyword in normalized for keyword in ("瘦脸针", "肉毒", "肉毒素", "除皱针")):
        return False
    has_history = any(
        keyword in normalized
        for keyword in (
            "之前",
            "以前",
            "前段时间",
            "上次",
            "那次",
            "打过",
            "打了",
            "打完",
            "之后",
        )
    )
    if not has_history:
        return False
    has_current_intent = any(
        keyword in normalized
        for keyword in (
            "这次",
            "今天",
            "现在想",
            "还想",
            "想打",
            "想做",
            "想要",
            "继续",
            "再打",
            "补打",
            "补一下",
            "要不要",
            "能不能",
            "可不可以",
            "多少钱",
            "价格",
            "单位",
            "方案",
            "保妥适",
            "乐提葆",
        )
    )
    if has_current_intent:
        return False
    return any(keyword in normalized for keyword in ("好很多", "效果", "不满意", "不好", "一般", "宽", "国字脸", "恢复"))


def _looks_like_prior_treatment_only_statement(text: str, keywords: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact or not _text_contains_any_keyword(compact, keywords):
        return False
    history_markers = (
        "之前",
        "以前",
        "上次",
        "那次",
        "刚做",
        "做过",
        "做了",
        "打过",
        "打了",
        "术后",
        "个月前",
        "年前",
        "长期打",
        "一年打",
        "每年打",
        "半年打",
    )
    has_history = any(marker in compact for marker in history_markers) or bool(
        re.search(r"(?:做|打)[一二三四五六七八九十两\d]+(?:次|支|针)", compact)
        or re.search(r"(?:每年|一年|半年|一个月|几个月).{0,8}(?:做|打)", compact)
    )
    if not has_history:
        return False
    current_intent_markers = (
        "现在想",
        "这次想",
        "今天想",
        "还想",
        "想要做",
        "想要修复",
        "想要调整",
        "要做修复",
        "想改善",
        "想修复",
        "想调整",
        "想重新",
        "想处理",
        "想解决",
        "想去掉",
        "想溶",
        "想取",
        "想做",
        "想打",
        "想调",
        "整体调",
        "继续处理",
        "继续做",
        "继续打",
        "再做",
        "再打",
        "补做",
        "补打",
        "现在还有",
        "现在还是",
        "现在仍然",
        "现在融完",
        "现在溶完",
        "一直还有",
        "还是有",
        "还有点",
        "融完后",
        "溶完后",
        "全空了",
        "都空了",
        "空了",
        "凹了",
        "塌了",
        "没了",
        "没有了",
        "残留",
        "没吸收",
        "没改善",
        "没做好",
        "没做满意",
        "不对称",
        "不自然",
    )
    return not any(marker in compact for marker in current_intent_markers)


def _looks_like_eyelid_shape_indication_statement(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    compact = re.sub(r"\s+", "", normalized)
    has_negated_double_eyelid = bool(
        re.search(r"(?:不想|不要|不考虑|不做|不是).{0,20}双眼皮", compact)
        or re.search(r"双眼皮.{0,12}(?:不想|不要|不考虑|不做|都不想)", compact)
    )
    has_strong_double_eyelid_repair_intent = bool(
        re.search(r"(?:想|要|考虑|打算|准备).{0,8}(?:做|割|修复|改善|调整).{0,8}双眼皮", compact)
        or re.search(r"双眼皮.{0,10}(?:修复|重做|重新做|变宽|变窄|塌陷|肉条|不满意|三角眼|手术)", compact)
        or any(keyword in compact for keyword in ("全切重睑", "大眼综合", "去皮去脂", "肌力矫正"))
    )
    if has_negated_double_eyelid and not has_strong_double_eyelid_repair_intent:
        return False
    has_positive_double_eyelid_action = bool(
        re.search(r"(?:想做|要做|考虑做|打算做|准备做|想割|要割|全切|埋线|修复|改善|调整).{0,8}双眼皮", compact)
        or re.search(r"双眼皮.{0,10}(?:修复|变宽|变窄|手术|效果|好吗|怎么样|多少钱|价格|恢复|方案|适合|可以)", compact)
        or any(keyword in compact for keyword in ("全切重睑", "大眼综合", "去皮去脂", "肌力矫正"))
    )
    if any(keyword in normalized for keyword in ("双眼皮", "重睑", "大眼综合", "去皮去脂", "肌力矫正")) and has_positive_double_eyelid_action:
        return True
    if any(keyword in normalized for keyword in ("单眼皮", "内双")):
        return any(
            keyword in normalized
            for keyword in (
                "我",
                "感觉",
                "觉得",
                "是个",
                "变成",
                "看久了",
                "贴",
                "眼皮",
                "眼睛",
                "改善",
                "面诊",
            )
        )
    if "美杜莎" in normalized:
        return any(keyword in normalized for keyword in ("预约", "奔着", "想看", "喜欢")) or (
            "眼型" in normalized and "设计" in normalized
        )
    if "眼型" in normalized:
        return any(keyword in normalized for keyword in ("预约", "奔着", "想看", "喜欢", "设计", "咨询"))
    return False


def _looks_like_negated_eye_bag_or_tear_trough(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    return bool(
        re.search(r"(?:没啥|没有|没|不明显|不算|还好).{0,8}(?:泪沟|眼袋)", compact)
        or re.search(r"(?:泪沟|眼袋).{0,8}(?:没啥|没有|没|不明显|不算|还好)", compact)
    )


def _looks_like_eye_injection_plastic_context(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    eye_terms = ("卧蚕", "泪沟", "眼周", "眼下", "眶周", "眼眶")
    injection_terms = ("打", "注射", "填充", "玻尿酸", "胶原", "胶原蛋白", "嗨体", "福曼", "复配", "套餐")
    surgery_terms = ("眼袋手术", "祛眼袋", "去眼袋", "内切", "外切", "眶隔释放", "眶隔脂肪")
    return any(term in compact for term in eye_terms) and any(term in compact for term in injection_terms) and not any(
        term in compact for term in surgery_terms
    )


def _primary_demand_required_evidence_groups(demand: str) -> tuple[tuple[str, ...], ...]:
    compact = re.sub(r"\s+", "", _clean_text(demand))
    if not compact:
        return tuple()

    groups: list[tuple[str, ...]] = []
    if any(keyword in compact for keyword in ("手部", "手上", "手的", "手疤", "手背", "手臂")):
        groups.append(("手部", "手上", "手的", "手背", "手臂"))
    if any(keyword in compact for keyword in ("口周", "嘴唇", "唇部", "唇形", "唇纹", "嘴角", "口下", "鼻基底", "鼻翼基底", "鼻子底", "鼻底")):
        groups.append(("口周", "嘴唇", "唇部", "唇形", "唇纹", "嘴角", "口下", "鼻基底", "鼻翼基底", "鼻子底", "鼻底"))
    if "残留" in compact:
        groups.append(("残留", "溶解", "溶掉", "吸收"))
    if "形态" in compact or "唇形" in compact:
        groups.append(("形态", "唇形", "没有形态", "小圆唇"))
    if "眼袋" in compact:
        groups.append(("眼袋",))
    if "泪沟" in compact or "眼下凹" in compact:
        groups.append(("泪沟", "眼下凹", "眼下凹陷", "凹陷", "凹的地方"))
    if any(keyword in compact for keyword in ("眼周凹陷", "眼眶空", "眼眶子空", "眼眶凹", "卧蚕")):
        groups.append(("眼周", "眼眶", "眼眶子", "卧蚕", "泪沟", "眼下凹", "凹陷", "空"))
    if any(keyword in compact for keyword in ("眶外C", "框外C", "外框C", "髋外C", "外科C", "外方C", "外方斜", "眉尾", "眉弓", "颞区")):
        groups.append(("眶外C", "眶外c", "框外C", "框外c", "外框C", "外框c", "髋外C", "髋外c", "外科C", "外科c", "外方C", "外方c", "外方斜", "眉尾", "眉弓", "颞区"))
    if any(keyword in compact for keyword in ("疲态", "疲惫", "没精神", "倦容")):
        groups.append(("疲态", "疲惫", "没精神", "倦容", "显老", "憔悴"))
    if "有神" in compact:
        groups.append(("有神", "没精神", "无神"))
    if any(keyword in compact for keyword in ("松弛", "下垂", "下垮", "脸垮", "老态", "紧致")):
        groups.append(("松弛", "下垂", "松垮", "下垮", "脸很垮", "脸也很垮", "脸垮", "很垮", "老态", "紧致"))
    if "拉皮修复" in compact or ("拉皮" in compact and "修复" in compact):
        groups.append(("拉皮", "小拉皮", "大拉皮"))
        groups.append(("修复", "没效果", "没有拉到", "又垮", "垮了"))
    if "鼻" in compact:
        groups.append(("鼻子", "鼻部", "鼻综合", "隆鼻", "山根", "鼻头", "鼻翼", "鼻孔", "鼻型", "鼻基底", "鼻翼基底", "鼻子底", "鼻底"))
    if any(keyword in compact for keyword in ("中面部", "面中", "八字纹", "衔接")):
        groups.append(("中面部", "面中", "八字纹", "鼻基底", "鼻翼基底", "鼻子底", "鼻底", "苹果肌", "衔接", "凹陷", "平整", "空"))
    if any(keyword in compact for keyword in ("后背", "背部", "吸脂", "抽脂", "超脂", "线条", "富贵包")):
        groups.append(("后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "线条", "富贵包"))
    if any(keyword in compact for keyword in ("痘印", "痘坑", "痘痘", "肤质")):
        groups.append(("痘印", "痘坑", "痘痘", "闭口", "肤质"))
    if "疤" in compact:
        groups.append(("疤痕", "疤", "留疤"))
    if any(keyword in compact for keyword in ("纹路", "细纹", "皱纹", "法令纹", "唇纹")):
        groups.append(("纹路", "细纹", "皱纹", "法令纹", "唇纹", "抬头纹", "鱼尾纹", "川字纹"))
    return tuple(groups)


def _primary_demand_evidence_covers_claim(demand: str, evidence: str) -> bool:
    groups = _primary_demand_required_evidence_groups(demand)
    if not groups:
        return True
    compact_evidence = re.sub(r"\s+", "", _clean_text(evidence))
    if not compact_evidence:
        return False
    return all(any(keyword in compact_evidence for keyword in group) for group in groups)


def _looks_like_wrinkle_tolerance_without_treatment_intent(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if not any(keyword in compact for keyword in ("纹路", "细纹", "皱纹", "法令纹", "唇纹")):
        return False
    tolerance_markers = (
        "能接受",
        "可以接受",
        "接受",
        "自然老去",
        "不追求完美",
        "没有追求完美",
        "保留一点点纹路",
        "不需要",
    )
    if not any(marker in compact for marker in tolerance_markers):
        return False
    explicit_treatment_markers = (
        "想改善纹路",
        "想改善细纹",
        "想改善皱纹",
        "想改善法令纹",
        "改善纹路",
        "改善细纹",
        "改善皱纹",
        "改善法令纹",
        "法令纹明显",
        "法令纹很深",
        "法令纹太深",
        "纹路明显",
        "纹路很深",
        "细纹明显",
        "皱纹明显",
        "去纹路",
        "去细纹",
        "去皱纹",
    )
    return not any(marker in compact for marker in explicit_treatment_markers)


def _primary_demand_is_wrinkle_texture(demand: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(demand))
    return any(keyword in compact for keyword in ("纹路", "细纹", "皱纹", "法令纹", "唇纹"))


def _line_content_without_speaker_prefix(text: str) -> str:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if "：" in compact:
        compact = compact.split("：")[-1]
    if ":" in compact:
        compact = compact.split(":")[-1]
    return compact


def _looks_like_wrinkle_texture_topic_answer_line(text: str) -> bool:
    content = _line_content_without_speaker_prefix(text).strip("，。！？,.!~～嗯啊哦呀哈吧呢啦")
    if not content:
        return False
    return bool(
        re.fullmatch(
            r"(?:就是|主要是|主要想看|想看|看一下|看看|我的|我这个|这个|那个|这边|这块|这儿|这里){0,2}"
            r"(?:法令纹|抬头纹|鱼尾纹|川字纹|眉间纹)"
            r"(?:问题|这块|这里|这边|这个)?",
            content,
        )
    )


def _looks_like_primary_demand_elicitation_line(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    return any(cue in compact for cue in _CONSULTATION_START_CUES) or bool(
        re.search(r"(?:主要|这次|今天).{0,8}(?:想|要|过来).{0,8}(?:了解|咨询|看).{0,8}(?:项目|哪方面|什么)", compact)
    )


def _looks_like_mechanism_explanation_without_customer_intent(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if _looks_like_direct_customer_primary_demand_line(compact):
        return False
    mechanism_cues = (
        "原因是",
        "随着年龄增长",
        "骨量流失",
        "胶原蛋白流失",
        "造成的",
        "形成的",
        "这个叫",
        "会显得",
    )
    if not any(cue in compact for cue in mechanism_cues):
        return False
    return any(keyword in compact for keyword in ("口周", "鼻基底", "法令纹", "婆婆纹", "凹陷", "松弛", "下垂", "显老"))


def _looks_like_nasolabial_base_solution_mechanism(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact or "法令纹" not in compact or "鼻基底" not in compact:
        return False
    if any(cue in compact for cue in ("我的鼻基底", "我鼻基底", "我想做鼻基底", "想改善鼻基底", "处理鼻基底")):
        return False
    return any(cue in compact for cue in ("解决法令纹", "常规情况下", "首先要解决", "先要解决", "深层", "这个地方的话"))


def _looks_like_explicit_wrinkle_texture_primary_demand(evidence: str) -> bool:
    lines = _evidence_text_lines(evidence)
    if not lines:
        return False
    wrinkle_terms = ("纹路", "细纹", "皱纹", "法令纹", "唇纹", "抬头纹", "鱼尾纹", "川字纹")
    intent_markers = (
        "想改善",
        "想处理",
        "想去",
        "想祛",
        "想淡化",
        "想打除皱",
        "想打肉毒",
        "想做除皱",
        "想解决",
        "想弄",
        "要改善",
        "要处理",
        "要去",
        "改善一下",
        "处理一下",
        "去掉",
        "祛除",
        "淡化",
        "打除皱",
        "除皱针",
        "打肉毒",
        "怎么改善",
        "怎么弄",
        "怎么办",
        "很在意",
        "比较在意",
        "介意",
        "困扰",
    )
    severity_markers = ("明显", "很深", "太深", "比较深", "严重", "重", "显老", "不好看")

    for line in lines:
        compact = re.sub(r"\s+", "", _clean_text(line))
        if not compact or not any(term in compact for term in wrinkle_terms):
            continue
        if _looks_like_wrinkle_texture_topic_answer_line(compact):
            return True
        if _looks_like_wrinkle_tolerance_without_treatment_intent(compact):
            continue
        if _looks_like_staff_extra_problem_or_recommendation(compact) or _looks_like_staff_explanatory_statement(compact):
            continue
        has_direct_intent = any(marker in compact for marker in intent_markers)
        has_strong_problem = any(marker in compact for marker in severity_markers) and any(
            cue in compact for cue in ("我", "自己", "脸上", "法令纹", "皱纹", "细纹", "纹路")
        )
        if has_direct_intent or has_strong_problem:
            return True

    for previous_line, current_line in zip(lines, lines[1:], strict=False):
        previous = re.sub(r"\s+", "", _clean_text(previous_line))
        current = _clean_text(current_line)
        if not previous or not any(term in previous for term in wrinkle_terms):
            if _looks_like_primary_demand_elicitation_line(previous) and _looks_like_wrinkle_texture_topic_answer_line(current):
                return True
            continue
        if _is_brief_customer_confirmation(current) and any(
            marker in previous for marker in ("想改善", "想处理", "主要想", "是不是想", "想不想", "要不要", "在意", "介意")
        ):
            return True

    return False


def _looks_like_sensitive_indication_statement(text: str) -> bool:
    normalized = _clean_text(text)
    compact = re.sub(r"\s+", "", normalized)
    if not compact:
        return False
    if any(cue in compact for cue in ("有的人", "如果你的", "如果您", "假如你的", "假如您")) and not any(
        cue in compact for cue in ("我敏感", "我就害怕敏感", "我怕敏感", "我皮肤敏感", "我变成敏感", "后面就敏感", "变成敏感肌")
    ):
        return False
    if "春季" in compact and not any(keyword in compact for keyword in ("敏感肌", "皮肤敏感", "肌肤敏感", "过敏", "泛红")):
        return False
    return bool(
        "敏感肌" in compact
        or "肌肤敏感" in compact
        or "皮肤敏感" in compact
        or re.search(r"(?:你|您|我|她|他|皮肤|肌肤).{0,8}(?:敏感|过敏|泛红)", compact)
    )


def _looks_like_dryness_indication_statement(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    dry_cues = ("干燥", "缺水", "补水", "保湿", "水润", "又干", "皮肤干", "比较干", "确实很干")
    waterlight_terms = ("水光", "水光针", "童颜水光")
    waterlight_intent_cues = (
        "做水光",
        "做个水光",
        "做一下水光",
        "打水光",
        "打个水光",
        "打这个基础水光",
        "做童颜水光",
        "打童颜水光",
        "水光方案",
        "水光项目",
        "补水水光",
    )
    if any(keyword in compact for keyword in waterlight_terms) and any(cue in compact for cue in waterlight_intent_cues):
        return True
    if any(keyword in compact for keyword in waterlight_terms) and not any(cue in compact for cue in dry_cues):
        return False
    return any(cue in compact for cue in dry_cues)


def _looks_like_rhinoplasty_indication_statement(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    provider_or_history_only = (
        "专门做鼻子" in compact
        or "做鼻子的" in compact
        or "做鼻子那里" in compact
        or "做鼻子那" in compact
    ) and not any(keyword in compact for keyword in ("我想做鼻子", "想做鼻子", "想咨询鼻综合", "想做鼻综合", "想隆鼻", "鼻头", "山根"))
    if provider_or_history_only:
        return False
    filler_context = ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "面中鼻基底", "玻尿酸", "润诺威", "润致", "瑞德喜", "几支", "3支", "一支", "填充", "凹陷", "凹了", "空了")
    surgical_context = (
        "鼻综合", "隆鼻", "鼻部塑形", "鼻塑形", "做鼻子", "鼻部方案", "鼻整形",
        "鼻型", "改善鼻型", "鼻头", "鼻翼", "鼻孔", "鼻背", "鼻尖", "山根", "膨体", "假体", "筋膜包裹",
        "驼峰鼻", "朝天鼻", "短鼻", "宽鼻", "Y鼻",
    )
    nasal_base_terms = ("鼻基底", "鼻翼基底", "鼻子底", "鼻底")
    if any(base in compact for base in nasal_base_terms) and any(keyword in compact for keyword in filler_context) and not any(
        keyword in compact for keyword in ("鼻综合", "隆鼻", "鼻整形", "鼻头", "鼻尖", "山根", "鼻背", "膨体", "假体")
    ):
        return False
    has_surgical_term = any(keyword in compact for keyword in surgical_context)
    if not has_surgical_term:
        return False
    direct_intent = bool(
        re.search(r"(?:我|本人|自己|这次|今天|现在|主要)?[^，。；;]{0,8}(?:想|要|考虑|打算|准备|咨询|改善|修复|做).{0,12}(?:鼻综合|隆鼻|鼻部塑形|鼻塑形|鼻部方案|鼻整形|鼻型|鼻头|鼻尖|鼻翼|鼻孔|鼻背|山根|膨体|假体|做鼻子)", compact)
        or re.search(r"(?:鼻综合|隆鼻|鼻部塑形|鼻塑形|鼻部方案|鼻整形|鼻型|鼻头|鼻尖|鼻翼|鼻孔|鼻背|山根|膨体|假体).{0,12}(?:想|要|考虑|打算|准备|咨询|改善|修复|方案|适合|可以|多少钱|价格|恢复)", compact)
    )
    staff_plan = _looks_like_staff_explanatory_statement(compact) and any(
        keyword in compact for keyword in ("适合做鼻综合", "鼻综合方案", "膨体会", "假体", "鼻头和山根", "改善鼻头", "改善山根")
    )
    return direct_intent or staff_plan


def _looks_like_filler_indication_statement(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    filler_terms = ("面部填充", "填充", "注射", "玻尿酸", "瑞德喜", "胶原", "苹果肌", "八字纹", "外轮廓线", "法令纹", "太阳穴", "鼻基底", "鼻翼基底", "鼻子底", "鼻底", "下巴", "凹陷", "凹了", "空了", "发空")
    if not any(term in compact for term in filler_terms):
        return False
    intent_terms = _INDICATION_INTENT_HINTS + ("想打", "要打", "重新打", "再打", "补一点", "调整", "修", "想恢复", "恢复平整", "平整一点", "空了", "发空", "凹了", "凹陷", "不平整")
    if any(term in compact for term in ("融了玻尿酸", "溶了玻尿酸", "以前打", "之前打", "打过")) and not any(
        term in compact for term in intent_terms
    ):
        return False
    if _looks_like_staff_explanatory_statement(compact) and not any(term in compact for term in intent_terms):
        return False
    return any(term in compact for term in intent_terms + _INDICATION_SELF_REPORT_HINTS)


def _looks_like_primary_demand_evidence(
    evidence: str,
    *,
    demand: str = "",
    allow_weak_staff_inference: bool = False,
) -> bool:
    lines = _evidence_text_lines(evidence)
    if not lines:
        return False
    all_text = "\n".join(lines)
    last_line = _clean_text(lines[-1])
    if len(lines) == 1 and _looks_like_topic_only_primary_demand_evidence(last_line):
        return False
    demand_keywords = _keywords_for_primary_demand(demand) if demand else tuple()
    if not demand_keywords and demand:
        demand_keywords = (demand,)
    if len(lines) >= 2 and _is_brief_customer_confirmation(last_line):
        previous_line = _clean_text(lines[-2])
        if _looks_like_staff_explanatory_statement(previous_line) and not _text_contains_any_keyword(
            previous_line,
            demand_keywords,
        ):
            return False
    if _looks_like_third_party_narrative_statement(last_line, keywords=demand_keywords):
        return False
    if _looks_like_prior_treatment_only_statement(last_line, demand_keywords):
        return False
    if _looks_like_staff_self_treatment_or_age_example(last_line) and not any(
        _looks_like_direct_customer_primary_demand_line(line, demand_keywords) for line in lines
    ):
        return False
    if "鼻基底" in demand and _looks_like_nasolabial_base_solution_mechanism(all_text):
        return False
    if any(marker in last_line for marker in ("不需要", "保留一点点纹路")) and any(keyword in last_line for keyword in ("法令纹", "纹路", "细纹")):
        return False
    if (
        ("纹路" in demand or "细纹" in demand or "皱纹" in demand)
        and _looks_like_wrinkle_tolerance_without_treatment_intent(last_line)
    ):
        return False
    if _primary_demand_is_wrinkle_texture(demand) and not _looks_like_explicit_wrinkle_texture_primary_demand(all_text):
        return False
    if _looks_like_staff_product_explanation_or_self_example(last_line):
        return False
    if _looks_like_staff_demo_or_example_statement(last_line):
        return False
    if _looks_like_staff_extra_problem_or_recommendation(last_line):
        return False
    if _looks_like_mechanism_explanation_without_customer_intent(last_line):
        return False
    if any(_looks_like_staff_extra_problem_or_recommendation(line) for line in lines) and not any(
        _looks_like_direct_customer_primary_demand_line(line, demand_keywords) for line in lines
    ):
        return False
    if (
        _looks_like_staff_explanatory_statement(last_line)
        and not allow_weak_staff_inference
        and not _looks_like_direct_customer_primary_demand_line(last_line, demand_keywords)
    ):
        return False
    if allow_weak_staff_inference and demand_keywords and not _text_contains_any_keyword(
        all_text,
        demand_keywords,
    ):
        return False
    if not _primary_demand_evidence_covers_claim(demand, all_text):
        return False
    return True


def _looks_like_topic_only_primary_demand_evidence(text: str) -> bool:
    compact = re.sub(r"[\s。！？!?，,、.]+", "", _clean_text(text))
    if not compact:
        return False
    return bool(
        re.fullmatch(
            r"(?:鼻子|鼻部|嘴巴|嘴唇|唇部|眼睛|眼部|眼袋|皮肤|脸|面部|下巴|胸|胸部|肩颈|后背|背部|手|手部)(?:呢|啊|呀|嘛|吗|吧)?",
            compact,
        )
    )


def _find_supported_primary_demand_evidence(
    segments: list[dict[str, Any]],
    *,
    existing_evidence: str,
    keywords: tuple[str, ...],
    excluded_keywords: tuple[str, ...],
    demand: str,
    allow_weak_staff_inference: bool = False,
) -> str | None:
    candidates: list[str] = []
    existing = _find_supported_evidence_from_existing_text(
        segments,
        existing_evidence=existing_evidence,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        allow_weak_staff_inference=allow_weak_staff_inference,
    )
    if existing:
        candidates.append(existing)
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if keywords and not _text_contains_any_keyword(text, keywords):
            continue
        supported = _supported_fact_source(
            segments,
            index,
            keywords=keywords,
            excluded_keywords=excluded_keywords,
            allow_weak_staff_inference=allow_weak_staff_inference,
        )
        if supported is not None:
            candidates.append(supported[1])
    valid_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_primary_demand_evidence(
            candidate,
            demand=demand,
            allow_weak_staff_inference=allow_weak_staff_inference,
        ):
            valid_candidates.append(candidate)
    if not valid_candidates:
        return None
    return max(valid_candidates, key=lambda candidate: _primary_demand_evidence_strength_score(candidate, keywords))


def _primary_demand_evidence_strength_score(evidence: str, keywords: tuple[str, ...]) -> int:
    lines = _evidence_text_lines(evidence)
    text = "\n".join(lines)
    last_line = _clean_text(lines[-1]) if lines else ""
    score = sum(1 for keyword in keywords if keyword and keyword in text)
    if any(cue in last_line for cue in _CUSTOMER_SELF_REPORT_HINTS + _INDICATION_SELF_REPORT_HINTS):
        score += 6
    if any(cue in last_line for cue in _INDICATION_INTENT_HINTS + ("重新打", "再打", "取还是不取", "做错", "凹陷")):
        score += 5
    if not _looks_like_staff_explanatory_statement(last_line):
        score += 2
    if len(lines) == 1:
        score += 1
    timestamp_match = re.search(r"\[(\d{2}):(\d{2})\]", evidence)
    if timestamp_match:
        seconds = int(timestamp_match.group(1)) * 60 + int(timestamp_match.group(2))
        if seconds <= 300 and _looks_like_direct_customer_primary_demand_line(last_line, keywords):
            score += 6
    return score


def _sync_chief_complaint_primary_demands(result_dict: dict[str, Any]) -> bool:
    primary_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
        if isinstance(item, dict)
    ]
    primary_demands = [
        demand
        for demand in (_clean_text(item.get("demand")) for item in primary_items)
        if demand
    ]
    consultation_result = _as_dict(result_dict.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    if not chief:
        return False

    changed = False
    summary = "；".join(primary_demands[:3])
    if _clean_text(chief.get("summary")) != summary:
        chief["summary"] = summary
        changed = True
    if _normalize_text_list(chief.get("primary_demands")) != primary_demands:
        chief["primary_demands"] = primary_demands
        changed = True
    if changed:
        consultation_result["chief_complaint_and_indications"] = chief
        result_dict["consultation_result"] = consultation_result
    return changed


def _standardized_indication_summary_item(item: dict[str, Any]) -> str:
    return (
        f"{_clean_text(item.get('department_name'))}（{_clean_text(item.get('department_code'))}）｜"
        f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('indication_code'))}）｜"
        f"{_clean_text(item.get('body_part_name'))}（{_clean_text(item.get('body_part_code'))}）"
    )


def _sync_chief_complaint_standardized_indications(result_dict: dict[str, Any]) -> bool:
    indication_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("standardized_indications")).get("items"))
        if isinstance(item, dict)
    ]
    indication_summaries = [_standardized_indication_summary_item(item) for item in indication_items]
    consultation_result = _as_dict(result_dict.get("consultation_result"))
    chief = _as_dict(consultation_result.get("chief_complaint_and_indications"))
    if not chief:
        return False
    if _normalize_text_list(chief.get("standardized_indications")) == indication_summaries:
        return False
    chief["standardized_indications"] = indication_summaries
    consultation_result["chief_complaint_and_indications"] = chief
    result_dict["consultation_result"] = consultation_result
    return True


_LIP_CONTEXT_HINTS = ("唇", "嘴唇", "嘴巴", "口周", "嘴角", "口下", "丰唇", "唇纹")
_EYE_CONTEXT_HINTS = ("眼", "眼袋", "泪沟", "眼下", "眶周", "双眼皮", "单眼皮", "提眉")
_NOSE_CONTEXT_HINTS = ("鼻", "山根", "鼻头", "鼻翼", "鼻尖", "鼻背", "鼻综合")
_FACE_CONTEXT_HINTS = ("面部", "中面部", "面中", "鼻基底", "鼻翼基底", "口下", "口周", "苹果肌", "八字纹", "外轮廓线", "法令纹", "太阳穴", "颞", "下巴", "全脸", "脸型", "轮廓", "凹陷", "松弛", "下垂", "抗衰")
_FILLER_CONTEXT_HINTS = ("面部填充", "填充", "注射", "玻尿酸", "瑞德喜", "胶原", "凹陷", "凹了", "空了", "苹果肌", "八字纹", "外轮廓线", "法令纹", "太阳穴", "颞", "鼻基底", "鼻翼基底", "下巴")
_BODY_CONTEXT_HINTS = ("身体", "腰腹", "大腿", "手臂", "肩颈", "斜方肌", "后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "富贵包", "疤")
_CHEST_CONTEXT_HINTS = ("胸", "乳房", "隆胸", "丰胸")
_NECK_CONTEXT_HINTS = ("颈", "脖子", "颈纹")


def _primary_demand_item_context(item: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean_text(item.get("demand")),
            _clean_text(item.get("body_part")),
            _clean_text(item.get("evidence")),
        )
        if part
    )


def _context_contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _indication_matches_primary_demand_item(indication_name: str, body_part_name: str, primary_item: dict[str, Any]) -> bool:
    indication_context = f"{_clean_text(indication_name)} {_clean_text(body_part_name)}"
    primary_context = _primary_demand_item_context(primary_item)
    if not indication_context or not primary_context:
        return False

    if _primary_item_is_nasolabial_fold_only(primary_item) and indication_name != "纹路":
        return False
    if indication_name == "面部填充":
        if _context_contains_any(primary_context, _NOSE_CONTEXT_HINTS) and not any(
            keyword in primary_context for keyword in ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "玻尿酸", "填充", "注射", "瑞德喜")
        ):
            return False
        return _context_contains_any(primary_context, _FILLER_CONTEXT_HINTS)
    if indication_name == "双眼皮":
        return _looks_like_eyelid_shape_indication_statement(primary_context)
    if indication_name == "鼻综合":
        return _looks_like_rhinoplasty_indication_statement(primary_context)
    if indication_name == "眼袋":
        return _context_contains_any(primary_context, ("眼袋", "泪沟", "眼下凹", "眼下凹陷", "眶隔"))
    if _context_contains_any(indication_context, _LIP_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _LIP_CONTEXT_HINTS)
    if _context_contains_any(indication_context, _EYE_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _EYE_CONTEXT_HINTS)
    if _context_contains_any(indication_context, _NOSE_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _NOSE_CONTEXT_HINTS)
    if _context_contains_any(indication_context, _CHEST_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _CHEST_CONTEXT_HINTS)
    if _context_contains_any(indication_context, _NECK_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _NECK_CONTEXT_HINTS)
    if _context_contains_any(indication_context, _BODY_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _BODY_CONTEXT_HINTS)
    if indication_name in {"面部除皱", "松弛下垂", "紧致淡纹"}:
        return _context_contains_any(primary_context, _FACE_CONTEXT_HINTS)

    hint = _indication_hint_for_name(indication_name)
    keywords = tuple(hint.get("keywords", ())) if hint else (indication_name,)
    return _text_contains_any_keyword(primary_context, keywords)


def _matching_primary_demand_for_indication(item: dict[str, Any], primary_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    indication_name = _clean_text(item.get("indication_name"))
    body_part_name = _clean_text(item.get("body_part_name"))
    if not indication_name:
        return None
    for primary_item in primary_items:
        if _indication_matches_primary_demand_item(indication_name, body_part_name, primary_item):
            return primary_item
    return None


def _primary_item_is_nasolabial_fold_only(item: dict[str, Any]) -> bool:
    demand = _clean_text(item.get("demand"))
    body_part = _clean_text(item.get("body_part"))
    evidence = _clean_text(item.get("evidence"))
    context = " ".join(part for part in (demand, body_part, evidence) if part)
    compact = re.sub(r"\s+", "", context)
    if "法令纹" not in compact:
        return False
    if any(keyword in demand for keyword in ("鼻基底", "面中支撑")):
        return False
    explicit_base_cues = (
        "我的鼻基底",
        "我鼻基底",
        "我想做鼻基底",
        "我想改善鼻基底",
        "想做鼻基底",
        "想改善鼻基底",
        "想处理鼻基底",
        "本次处理鼻基底",
    )
    if any(cue in compact for cue in explicit_base_cues):
        return False
    return True


def _build_nasolabial_fold_indication_item(primary_item: dict[str, Any]) -> dict[str, Any] | None:
    matched = resolve_indication_reference_item(
        department_name="皮肤",
        indication_name="纹路",
        body_part_name="面部",
    )
    if matched is None:
        return None
    evidence = _clean_text(primary_item.get("evidence"))
    if not evidence:
        return None
    return {
        "department_code": matched.department_code,
        "department_name": matched.department_name,
        "indication_code": matched.indication_code,
        "indication_name": matched.indication_name,
        "body_part_code": matched.body_part_code,
        "body_part_name": matched.body_part_name,
        "evidence": evidence,
    }


def _collapse_nasolabial_fold_related_indications(
    items: list[dict[str, Any]],
    primary_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    nasolabial_primary_items = [item for item in primary_items if _primary_item_is_nasolabial_fold_only(item)]
    if len(primary_items) != 1 or len(nasolabial_primary_items) != 1:
        return items, False

    primary_item = nasolabial_primary_items[0]
    wrinkle_items = [
        item
        for item in items
        if _clean_text(item.get("indication_name")) == "纹路" and _clean_text(item.get("body_part_name")) == "面部"
    ]
    selected = dict(wrinkle_items[0]) if wrinkle_items else _build_nasolabial_fold_indication_item(primary_item)
    if selected is None:
        return items, False

    primary_evidence = _clean_text(primary_item.get("evidence"))
    if primary_evidence and _clean_text(selected.get("evidence")) != primary_evidence:
        selected["evidence"] = primary_evidence

    collapsed = [selected]
    return collapsed, len(items) != 1 or items[0] != selected


def _append_nasal_base_primary_demand_if_supported(
    items: list[dict[str, Any]],
    *,
    segments: list[dict[str, Any]],
    staff_recommendations_payload: dict[str, Any] | None,
) -> bool:
    if len(items) >= 3:
        return False
    if any(
        any(keyword in _primary_demand_item_context(item) for keyword in ("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "八字纹"))
        for item in items
    ):
        return False

    recommendation_context = ""
    if isinstance(staff_recommendations_payload, dict):
        recommendation_context = "\n".join(
            _recommendation_text(item)
            for item in _as_list(staff_recommendations_payload.get("items"))
            if isinstance(item, dict)
        )
    if recommendation_context and not _looks_like_face_filler_context(recommendation_context):
        return False

    demand = "改善鼻基底/中面部衔接，希望恢复平整自然"
    evidence = _find_supported_primary_demand_evidence(
        segments,
        existing_evidence="",
        keywords=("鼻基底", "鼻翼基底", "鼻子底", "鼻底", "中面部", "面中", "八字纹"),
        excluded_keywords=(),
        demand=demand,
    )
    if evidence is None and recommendation_context:
        evidence = _recommendation_evidence_matching(staff_recommendations_payload, _looks_like_face_filler_context)
    if not evidence or not _looks_like_primary_demand_evidence(
        evidence,
        demand=demand,
        allow_weak_staff_inference=bool(recommendation_context),
    ):
        return False
    items.append(
        {
            "priority": len(items) + 1,
            "demand": demand,
            "body_part": "鼻基底/面中",
            "evidence": evidence,
        }
    )
    return True


def _profile_tag_match_config(category: str, value: str) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    if category == "治疗项目":
        if value == "无医美史":
            return tuple(), _NO_PRIOR_TREATMENT_TAG_PATTERNS
        for candidate, keywords in _TREATMENT_HISTORY_HINTS:
            if value == candidate:
                return keywords, tuple()
        return (value,), tuple()
    if category == "历史用的设备/原材料名称":
        return (value,), tuple()
    if category == "健康风险/禁忌":
        if value == "无风险禁忌":
            return _NEGATIVE_HEALTH_HINTS, tuple()
        for candidate, keywords in _HEALTH_TAG_HINTS:
            if value == candidate:
                return keywords, tuple()
        return (value,), tuple()
    if category in {"出生日期", "出生日期/年龄"}:
        return tuple(), _BIRTHDATE_PATTERNS
    if category == "常驻城市":
        if value == "本地":
            return _LOCAL_CITY_SELF_REPORT_CUES, tuple()
        if value == "外地":
            return _NON_LOCAL_CITY_SELF_REPORT_CUES, tuple()
    if category == "价格敏感度":
        for candidate, keywords in _PRICE_SENSITIVITY_HINTS:
            if value == candidate:
                return keywords, tuple()
    if category == "特殊身份":
        for candidate, keywords in _SPECIAL_IDENTITY_HINTS:
            if value == candidate:
                return keywords, tuple()
    if category == "负面项目/设备/原材料":
        if value == "无":
            return ("没有", "没", "无", "踩雷", "翻车", "失败", "不满意", "负面", "过敏", "后悔", "项目", "设备", "材料", "原材料"), tuple()
        return (value,), tuple()
    if category == "个人情况":
        if value == "已婚":
            return ("我老公", "我丈夫", "我老婆", "我妻子", "已婚"), tuple()
        if value == "有恋人":
            return ("我男朋友", "我女朋友", "我对象", "我恋人", "有对象"), tuple()
        if value == "单身":
            return ("单身",), tuple()
    if category == "决策主体":
        if value == "伴侣":
            return ("老公", "丈夫", "老婆", "妻子", "男朋友", "女朋友", "对象", "恋人"), tuple()
        if value == "父母":
            return ("妈妈", "母亲", "爸爸", "父母"), tuple()
        if value == "自主":
            return ("自己决定", "我自己定", "我自己做主", "我自己说了算"), tuple()
    if category == "亲属/子女情况":
        return ("无孩", "没孩子", "未育", "一孩", "一个孩子", "一个娃", "二孩", "两个孩子", "2孩", "2个孩子", "三孩"), tuple()
    if category == "倾向回访方式":
        if value == "微信":
            return ("微信",), tuple()
        if value == "电话":
            return ("电话",), tuple()
        if value == "短信":
            return ("短信",), tuple()
    return (value,), tuple()


def _sanitize_customer_primary_demands(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    payload = _as_dict(result_dict.get("customer_primary_demands"))
    items = [item for item in _as_list(payload.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    segments = _consultation_segments(raw)
    if not segments:
        return False

    kept: list[dict[str, Any]] = []
    weak_candidates: list[dict[str, Any]] = []
    staff_recommendations_payload = _as_dict(result_dict.get("staff_recommendations"))
    allow_weak_fallback = _allows_main_fact_floor(
        segments,
        staff_recommendations_payload=staff_recommendations_payload,
    )
    changed = False
    for item in items:
        demand = _clean_text(item.get("demand"))
        if not demand:
            changed = True
            continue
        if not _clean_text(item.get("body_part")) and any(keyword in demand for keyword in _PRIMARY_DEMAND_LOGISTICS_HINTS):
            changed = True
            continue
        keywords = _keywords_for_primary_demand(demand)
        excluded_keywords = _excluded_keywords_for_primary_demand(demand)
        evidence = _find_supported_primary_demand_evidence(
            segments,
            existing_evidence=_clean_text(item.get("evidence")),
            keywords=keywords or (demand,),
            excluded_keywords=excluded_keywords,
            demand=demand,
        )
        if evidence is None or not _looks_like_primary_demand_evidence(evidence, demand=demand):
            if allow_weak_fallback:
                weak_evidence = _find_supported_primary_demand_evidence(
                    segments,
                    existing_evidence=_clean_text(item.get("evidence")),
                    keywords=keywords or (demand,),
                    excluded_keywords=excluded_keywords,
                    demand=demand,
                    allow_weak_staff_inference=True,
                )
                if weak_evidence and _looks_like_primary_demand_evidence(
                    weak_evidence,
                    demand=demand,
                    allow_weak_staff_inference=True,
                ):
                    weak_candidates.append({**item, "evidence": weak_evidence})
            changed = True
            continue
        updated = dict(item)
        if evidence != _clean_text(item.get("evidence")):
            changed = True
        updated["evidence"] = evidence
        original_evidence = _clean_text(item.get("evidence"))
        if _should_naturalize_existing_primary_demand(
            demand,
            evidence=evidence,
            original_evidence=original_evidence,
        ):
            natural_demand = _naturalize_primary_demand(
                demand,
                body_part=_clean_text(updated.get("body_part")) or None,
                evidence=evidence,
            )
            if natural_demand and natural_demand != demand:
                updated["demand"] = natural_demand
                changed = True
        natural_body_part = _naturalize_primary_body_part(
            _clean_text(updated.get("body_part")) or None,
            evidence=evidence,
        )
        if natural_body_part != _clean_text(updated.get("body_part")):
            updated["body_part"] = natural_body_part
            changed = True
        kept.append(updated)

    kept, deduped_changed = _dedupe_primary_demand_items(kept)
    changed = changed or deduped_changed
    nasal_base_changed = _append_nasal_base_primary_demand_if_supported(
        kept,
        segments=segments,
        staff_recommendations_payload=staff_recommendations_payload,
    )
    if nasal_base_changed:
        kept, nasal_base_deduped_changed = _dedupe_primary_demand_items(kept)
        changed = changed or nasal_base_deduped_changed or nasal_base_changed
    if not kept:
        backfilled_payload: dict[str, Any] = {"items": []}
        if _backfill_primary_demands(backfilled_payload, segments=segments):
            backfilled_items = [
                item
                for item in _as_list(backfilled_payload.get("items"))
                if isinstance(item, dict) and _looks_like_primary_demand_evidence(
                    _clean_text(item.get("evidence")),
                    demand=_clean_text(item.get("demand")),
                )
            ]
            if backfilled_items:
                kept, _backfill_dedupe_changed = _dedupe_primary_demand_items(backfilled_items)
                kept = kept[:3]
                changed = True
    if not kept and allow_weak_fallback and weak_candidates:
        weak_kept, _weak_changed = _dedupe_primary_demand_items(weak_candidates)
        if weak_kept:
            kept = weak_kept[:1]
            changed = True

    if not changed and len(kept) == len(items):
        if all(_clean_text(item.get("evidence")) for item in kept):
            return _sync_chief_complaint_primary_demands(result_dict)

    for priority, item in enumerate(kept, start=1):
        item["priority"] = priority
    payload["items"] = kept
    payload["summary"] = "；".join(_clean_text(item.get("demand")) for item in kept[:3])
    result_dict["customer_primary_demands"] = payload
    _sync_chief_complaint_primary_demands(result_dict)
    return True


def _backfill_empty_customer_primary_demands(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    payload = _as_dict(result_dict.get("customer_primary_demands"))
    if _as_list(payload.get("items")):
        return False
    segments = _consultation_segments(raw)
    if not segments:
        return False

    backfilled_payload: dict[str, Any] = {"items": []}
    if not _backfill_primary_demands(backfilled_payload, segments=segments):
        lift_repair_evidence = _find_lift_repair_primary_demand_evidence(segments)
        if not lift_repair_evidence:
            return False
        backfilled_payload["items"] = [
            {
                "priority": 1,
                "demand": "想做拉皮修复，改善既往拉皮效果不佳",
                "body_part": "面部",
                "evidence": lift_repair_evidence,
            }
        ]
    backfilled_items = [
        item
        for item in _as_list(backfilled_payload.get("items"))
        if isinstance(item, dict) and _looks_like_primary_demand_evidence(
            _clean_text(item.get("evidence")),
            demand=_clean_text(item.get("demand")),
        )
    ]
    if not backfilled_items:
        lift_repair_evidence = _find_lift_repair_primary_demand_evidence(segments)
        if not lift_repair_evidence:
            return False
        backfilled_items = [
            {
                "priority": 1,
                "demand": "想做拉皮修复，改善既往拉皮效果不佳",
                "body_part": "面部",
                "evidence": lift_repair_evidence,
            }
        ]
    kept, _dedupe_changed = _dedupe_primary_demand_items(backfilled_items)
    if not kept:
        return False
    kept = kept[:3]
    for priority, item in enumerate(kept, start=1):
        item["priority"] = priority
    payload["items"] = kept
    payload["summary"] = "；".join(_clean_text(item.get("demand")) for item in kept)
    result_dict["customer_primary_demands"] = payload
    _sync_chief_complaint_primary_demands(result_dict)
    return True


def _find_lift_repair_primary_demand_evidence(segments: list[dict[str, Any]]) -> str | None:
    demand = "想做拉皮修复，改善既往拉皮效果不佳"
    for segment in segments[_find_consultation_start_index(segments):]:
        text = _clean_text(segment.get("text"))
        compact = re.sub(r"\s+", "", text)
        if not compact or "拉皮" not in compact:
            continue
        if not any(cue in compact for cue in ("想要做修复", "想修复", "做修复", "没有拉到", "没效果", "又垮")):
            continue
        evidence = _segment_evidence(segment)
        if _looks_like_primary_demand_evidence(
            evidence,
            demand=demand,
            allow_weak_staff_inference=True,
        ):
            return evidence
    return None


def _sanitize_standardized_indications(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    payload = _as_dict(result_dict.get("standardized_indications"))
    items = [item for item in _as_list(payload.get("items")) if isinstance(item, dict)]
    if not items:
        forced_items = _standardized_indication_items_from_primary_demands(
            _as_dict(result_dict.get("customer_primary_demands")),
        )
        if forced_items:
            payload["items"] = forced_items
            summary_names = [
                f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('body_part_name'))}）"
                for item in forced_items[:6]
            ]
            payload["summary"] = f"识别出{len(forced_items)}项适应症：" + "；".join(summary_names)
            result_dict["standardized_indications"] = payload
            _sync_chief_complaint_standardized_indications(result_dict)
            return True
        return False
    segments = _consultation_segments(raw)
    if not segments:
        return False

    primary_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
        if isinstance(item, dict)
    ]
    kept: list[dict[str, Any]] = []
    weak_candidates: list[dict[str, Any]] = []
    staff_recommendations_payload = _as_dict(result_dict.get("staff_recommendations"))
    allow_weak_fallback = _allows_main_fact_floor(
        segments,
        staff_recommendations_payload=staff_recommendations_payload,
    )
    changed = False
    for item in items:
        indication_name = _clean_text(item.get("indication_name"))
        if not indication_name:
            changed = True
            continue
        matched_primary = _matching_primary_demand_for_indication(item, primary_items)
        recommendation_evidence = _recommendation_evidence_for_indication(
            indication_name,
            staff_recommendations_payload,
        )
        if (
            primary_items
            and matched_primary is None
            and not (
                recommendation_evidence
                and _looks_like_indication_evidence(
                    indication_name,
                    recommendation_evidence,
                    allow_weak_staff_inference=True,
                )
            )
        ):
            changed = True
            continue
        hint = _indication_hint_for_name(indication_name)
        keywords = tuple(hint.get("keywords", ())) if hint else (indication_name,)
        excluded_keywords = tuple(hint.get("excluded_keywords", ())) if hint else ()
        primary_evidence = _clean_text(matched_primary.get("evidence")) if matched_primary else ""
        evidence = primary_evidence if primary_evidence and _looks_like_indication_evidence(indication_name, primary_evidence) else None
        evidence = evidence or _find_supported_evidence_from_existing_text(
            segments,
            existing_evidence=_clean_text(item.get("evidence")),
            keywords=keywords,
            excluded_keywords=excluded_keywords,
        ) or _find_supported_evidence_for_keywords(
            segments,
            keywords=keywords,
            excluded_keywords=excluded_keywords,
        )
        if evidence is None or not _looks_like_indication_evidence(indication_name, evidence):
            if recommendation_evidence and _looks_like_indication_evidence(
                indication_name,
                recommendation_evidence,
                allow_weak_staff_inference=True,
            ):
                updated = dict(item)
                updated["evidence"] = recommendation_evidence
                kept.append(updated)
                changed = True
                continue
            if allow_weak_fallback:
                weak_evidence = _find_supported_evidence_from_existing_text(
                    segments,
                    existing_evidence=_clean_text(item.get("evidence")),
                    keywords=keywords,
                    excluded_keywords=excluded_keywords,
                    allow_weak_staff_inference=True,
                ) or _find_supported_evidence_for_keywords(
                    segments,
                    keywords=keywords,
                    excluded_keywords=excluded_keywords,
                    allow_weak_staff_inference=True,
                )
                if weak_evidence and _looks_like_indication_evidence(
                    indication_name,
                    weak_evidence,
                    allow_weak_staff_inference=True,
                ):
                    weak_item = {**item, "evidence": weak_evidence}
                    if recommendation_evidence and _looks_like_indication_evidence(
                        indication_name,
                        recommendation_evidence,
                        allow_weak_staff_inference=True,
                    ):
                        weak_item["evidence"] = recommendation_evidence
                        kept.append(weak_item)
                    else:
                        weak_candidates.append(weak_item)
            changed = True
            continue
        updated = dict(item)
        if recommendation_evidence and _looks_like_indication_evidence(
            indication_name,
            recommendation_evidence,
            allow_weak_staff_inference=allow_weak_fallback,
        ):
            if recommendation_evidence != evidence:
                changed = True
            evidence = recommendation_evidence
        updated["evidence"] = evidence
        kept.append(updated)

    if not kept and allow_weak_fallback and weak_candidates:
        kept = weak_candidates[:1]
        changed = True

    if not kept:
        forced_items = _standardized_indication_items_from_primary_demands(
            _as_dict(result_dict.get("customer_primary_demands")),
        )
        if forced_items:
            kept = forced_items[:1]
            changed = True

    if any(
        _clean_text(item.get("indication_name")) == "鼻综合" and _clean_text(item.get("body_part_name")) == "鼻部"
        for item in kept
    ):
        filtered_kept = []
        for item in kept:
            if _clean_text(item.get("indication_name")) in {"鼻翼整形", "鼻修复"} and _clean_text(item.get("body_part_name")) == "鼻部":
                changed = True
                continue
            filtered_kept.append(item)
        kept = filtered_kept

    kept, collapsed_changed = _collapse_nasolabial_fold_related_indications(kept, primary_items)
    changed = changed or collapsed_changed
    kept, augmented_changed = _augment_high_confidence_indications(
        kept,
        segments=segments,
        primary_items=primary_items,
        staff_recommendations_payload=staff_recommendations_payload,
    )
    changed = changed or augmented_changed

    if not changed and len(kept) == len(items):
        if all(_clean_text(item.get("evidence")) for item in kept):
            return _sync_chief_complaint_standardized_indications(result_dict)

    payload["items"] = kept
    summary_names = [f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('body_part_name'))}）" for item in kept[:6]]
    payload["summary"] = f"识别出{len(kept)}项适应症：" + "；".join(summary_names) if kept else "未识别出明确适应症。"
    result_dict["standardized_indications"] = payload
    _sync_chief_complaint_standardized_indications(result_dict)
    return True


def _extract_money_text(text: str) -> str | None:
    match = _MONEY_TEXT_RE.search(text)
    return match.group(1) if match else None


def _append_decision_factor(factors: list[str], factor: str) -> bool:
    normalized = _clean_text(factor)
    if not normalized or normalized in factors:
        return False
    factors.append(normalized)
    return True


def _text_implies_family_decision(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    return any(relation in normalized for relation in _FAMILY_DECISION_RELATION_HINTS) and any(
        action in normalized for action in _FAMILY_DECISION_ACTION_HINTS
    )


def _concern_implied_decision_factors(concern_texts: list[str]) -> set[str]:
    inferred: set[str] = set()
    for text in concern_texts:
        normalized = _clean_text(text)
        if not normalized:
            continue
        for factor, keywords in _DECISION_FACTOR_FROM_CONCERN_HINTS.items():
            if any(keyword in normalized for keyword in keywords):
                inferred.add(factor)
    return inferred


# These labels are subjective concern categories. They should always live in
# customer_concerns and never re-appear under "其他影响因素".
_CONCERN_CATEGORY_LABELS = {"价格", "恢复期", "效果", "疼痛", "风险", "家庭决策", "对比机构"}
_OBJECTIVE_DECISION_FACTOR_KEYWORDS = (
    "生理期",
    "经期",
    "月经",
    "姨妈",
    "例假",
    "妊娠",
    "怀孕",
    "备孕",
    "哺乳",
    "禁忌",
    "身体条件",
    "竞对",
    "竞品",
    "同行机构",
    "黑名单",
    "特殊身份",
    "支付",
    "流程",
    "系统",
    "扫码",
    "到院",
    "路程",
    "外地",
    "高铁",
    "飞机",
    "赶时间",
)
_SUBJECTIVE_DECISION_FACTOR_KEYWORDS = (
    "价格",
    "预算",
    "太贵",
    "恢复",
    "肿",
    "效果",
    "自然",
    "疼",
    "风险",
    "副作用",
    "安全",
    "商量",
    "考虑",
    "对比",
    "家人",
    "老公",
    "男朋友",
)


def _looks_like_subjective_decision_factor(text: str) -> bool:
    normalized = _clean_text(text)
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _OBJECTIVE_DECISION_FACTOR_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in _SUBJECTIVE_DECISION_FACTOR_KEYWORDS)


def _filter_overlapping_decision_factors(
    factors: list[str],
    *,
    concern_texts: list[str],
    evidence_texts: list[str],
    loss_reasons: list[str],
) -> list[str]:
    concern_implied = _concern_implied_decision_factors(concern_texts)
    has_family_decision_evidence = any(
        _text_implies_family_decision(text)
        for text in [*concern_texts, *evidence_texts, *loss_reasons]
    )
    filtered: list[str] = []
    for factor in factors:
        normalized = _clean_text(factor)
        if not normalized:
            continue
        # Drop concern-category labels — they belong in customer_concerns.
        if normalized in _CONCERN_CATEGORY_LABELS:
            continue
        if _looks_like_subjective_decision_factor(normalized):
            continue
        if normalized in concern_implied:
            continue
        if normalized == "家庭决策" and not has_family_decision_evidence:
            continue
        if normalized not in filtered:
            filtered.append(normalized)
    return filtered


def _append_concern(
    items: list[dict[str, Any]],
    *,
    concern_type: str,
    content: str,
    evidence: str,
) -> bool:
    normalized_content = _clean_text(content)
    if not normalized_content or not evidence:
        return False
    existing = {
        (_clean_text(item.get("type")), _clean_text(item.get("content")))
        for item in items
        if isinstance(item, dict)
    }
    key = (_clean_text(concern_type), normalized_content)
    if key in existing:
        return False
    items.append(
        {
            "type": concern_type,
            "content": normalized_content,
            "evidence": evidence,
        }
    )
    return True


def _infer_demand_priority(
    primary_items: list[dict[str, Any]],
    *,
    body_part: str | None,
    text: str,
) -> list[int]:
    priorities: list[int] = []
    normalized_body_part = _clean_text(body_part)
    normalized_text = _clean_text(text)
    for item in primary_items:
        if not isinstance(item, dict):
            continue
        priority = item.get("priority")
        if not isinstance(priority, int):
            continue
        item_body_part = _clean_text(item.get("body_part"))
        item_demand = _clean_text(item.get("demand"))
        if normalized_body_part and item_body_part and (
            normalized_body_part == item_body_part
            or normalized_body_part in item_body_part
            or item_body_part in normalized_body_part
            or (
                any(keyword in normalized_body_part for keyword in ("唇", "口周", "嘴"))
                and any(keyword in item_body_part for keyword in ("唇", "口周", "嘴"))
            )
        ):
            priorities.append(priority)
            continue
        if _recommendation_matches_primary_demand_item(
            {"body_part": normalized_body_part, "recommendation": normalized_text, "product_or_solution": normalized_text},
            item,
        ):
            priorities.append(priority)
            continue
        if item_demand and (
            item_demand in normalized_text
            or any(token in normalized_text for token in item_demand.split("，"))
        ):
            priorities.append(priority)
    deduped: list[int] = []
    for priority in priorities:
        if priority not in deduped:
            deduped.append(priority)
    return deduped


def _append_profile_tag(
    tags: list[dict[str, Any]],
    *,
    category: str,
    value: str,
    evidence: str,
    weight_by_category: dict[str, int],
) -> bool:
    canonical_category = canonicalize_profile_tag_category(category)
    if canonical_category is None:
        return False
    canonical_value = canonicalize_profile_tag_value(canonical_category, value)
    if canonical_value is None or not is_valid_profile_tag_value(canonical_category, canonical_value):
        return False
    dedupe_key = (canonical_category, canonical_value)
    existing = {
        (
            canonicalize_profile_tag_category(item.get("category")),
            canonicalize_profile_tag_value(item.get("category"), item.get("value")),
        )
        for item in tags
        if isinstance(item, dict)
    }
    if dedupe_key in existing:
        return False
    tags.append(
        {
            "category": canonical_category,
            "value": canonical_value,
            "weight_level": weight_by_category.get(canonical_category),
            "evidence": evidence or None,
        }
    )
    return True


def _append_history_device_profile_tags(
    tags: list[dict[str, Any]],
    *,
    value: str,
    evidence: str,
    weight_by_category: dict[str, int],
) -> bool:
    changed = _append_profile_tag(
        tags,
        category="历史用的设备/原材料名称",
        value=value,
        evidence=evidence,
        weight_by_category=weight_by_category,
    )
    if value in _INJECTABLE_HISTORY_DEVICE_HINTS:
        changed = _append_profile_tag(
            tags,
            category="治疗项目",
            value="注射类",
            evidence=evidence,
            weight_by_category=weight_by_category,
        ) or changed
    return changed


def _normalize_explicit_no_prior_treatment_profile_tags(
    tags: list[dict[str, Any]],
    *,
    weight_by_category: dict[str, int],
) -> tuple[list[dict[str, Any]], bool]:
    explicit_no_prior: dict[str, Any] | None = None
    for item in tags:
        category = canonicalize_profile_tag_category(item.get("category"))
        if category != "治疗项目":
            continue
        value = canonicalize_profile_tag_value(category, item.get("value"))
        if value == "无医美史":
            explicit_no_prior = {
                **item,
                "category": "治疗项目",
                "value": "无医美史",
                "weight_level": weight_by_category.get("治疗项目"),
            }
            break
    if explicit_no_prior is None:
        return tags, False

    has_positive_treatment_history = any(
        not _looks_like_no_prior_treatment_statement(_clean_text(item.get("evidence")))
        and (
            (
                canonicalize_profile_tag_category(item.get("category")) == "治疗项目"
                and canonicalize_profile_tag_value("治疗项目", item.get("value")) != "无医美史"
            )
            or (
                canonicalize_profile_tag_category(item.get("category")) == "历史用的设备/原材料名称"
                and canonicalize_profile_tag_value("历史用的设备/原材料名称", item.get("value")) != "无"
            )
        )
        for item in tags
        if isinstance(item, dict)
    )
    if has_positive_treatment_history:
        filtered: list[dict[str, Any]] = []
        changed = False
        for item in tags:
            category = canonicalize_profile_tag_category(item.get("category"))
            value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
            if category == "治疗项目" and value == "无医美史":
                changed = True
                continue
            if category == "历史用的设备/原材料名称" and value == "无":
                changed = True
                continue
            filtered.append(item)
        return filtered, changed

    normalized: list[dict[str, Any]] = []
    dependent_defaults: dict[str, dict[str, Any]] = {}
    insert_index: int | None = None
    changed = False
    default_evidence = explicit_no_prior.get("evidence") or None

    for item in tags:
        category = canonicalize_profile_tag_category(item.get("category"))
        value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
        if category == "治疗项目":
            if value == "无医美史":
                if insert_index is None:
                    normalized.append(explicit_no_prior)
                    insert_index = len(normalized)
                else:
                    changed = True
                continue
            changed = True
            continue
        if category == "历史用的设备/原材料名称":
            if value == "无" and category not in dependent_defaults:
                dependent_defaults[category] = {
                    **item,
                    "category": category,
                    "value": "无",
                    "weight_level": weight_by_category.get(category),
                    "evidence": item.get("evidence") or default_evidence,
                }
            else:
                changed = True
            continue
        normalized.append(item)

    if insert_index is None:
        return tags, False

    for offset, category in enumerate(("历史用的设备/原材料名称",)):
        if category not in dependent_defaults:
            dependent_defaults[category] = {
                "category": category,
                "value": "无",
                "weight_level": weight_by_category.get(category),
                "evidence": default_evidence,
            }
            changed = True
        normalized.insert(insert_index + offset, dependent_defaults[category])

    return normalized, changed


def _backfill_customer_profile_tags(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    customer_profile = _as_dict(result_dict.setdefault("customer_profile", {}))
    existing_tags = [
        item
        for item in _as_list(customer_profile.get("tags"))
        if isinstance(item, dict)
    ]
    segments = _consultation_segments(raw)
    if not segments:
        return False

    weight_by_category = _profile_weight_by_category()
    tags: list[dict[str, Any]] = list(existing_tags)
    age = _normalize_age_value(customer_profile.get("age"))
    age_evidence = _clean_text(customer_profile.get("age_evidence"))
    changed = False

    for index, segment in enumerate(segments):
        supported = _supported_fact_source(
            segments,
            index,
            keywords=(
                "微信",
                "外地",
                "本地",
                "成都",
                "郫都",
                "郫县",
                "双流",
                "新都",
                "武侯",
                "锦江",
                "青羊",
                "金牛",
                "高新",
                "老公",
                "丈夫",
                "老婆",
                "妻子",
                "男朋友",
                "女朋友",
                "恋人",
                "妈妈",
                "母亲",
                "爸爸",
                "父母",
                "自己决定",
                "我自己定",
                "我自己做主",
                "我自己说了算",
                "无孩",
                "没孩子",
                "未育",
                "一孩",
                "一个孩子",
                "一个娃",
                "二孩",
                "两个孩子",
                "2孩",
                "2个孩子",
                "三孩",
                "年龄",
                "多大",
                "几岁",
                "多少岁",
            )
            + _NEGATIVE_HEALTH_HINTS
            + tuple(keyword for _, keywords in _HEALTH_TAG_HINTS for keyword in keywords)
            + tuple(keyword for _, keywords in _TREATMENT_HISTORY_HINTS for keyword in keywords)
            + _HISTORY_DEVICE_HINTS
            + tuple(keyword for _, keywords in _PRICE_SENSITIVITY_HINTS for keyword in keywords)
            + tuple(keyword for _, keywords in _SPECIAL_IDENTITY_HINTS for keyword in keywords),
            patterns=_NO_PRIOR_TREATMENT_TAG_PATTERNS + _BIRTHDATE_PATTERNS + (_AGE_VALUE_RE,),
        )
        if supported is None:
            for keyword in _HISTORY_DEVICE_HINTS:
                history_device_source = _supported_history_device_source(segments, index, keyword=keyword)
                if history_device_source:
                    _, history_device_evidence = history_device_source
                    changed = _append_history_device_profile_tags(
                        tags,
                        value=keyword,
                        evidence=history_device_evidence,
                        weight_by_category=weight_by_category,
                    ) or changed
            continue
        text, evidence = supported

        if _looks_like_no_prior_treatment_statement(evidence):
            changed = _append_profile_tag(
                tags,
                category="治疗项目",
                value="无医美史",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        if "微信" in text and any(hint in text for hint in _WECHAT_FOLLOW_UP_HINTS):
            changed = _append_profile_tag(
                tags,
                category="倾向回访方式",
                value="微信",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        supported_age = _extract_supported_age(text)
        if supported_age and (not age or supported_age != age):
            age = supported_age
            age_evidence = evidence
            changed = True

        for pattern in _BIRTHDATE_PATTERNS:
            match = pattern.search(text)
            if match and _looks_like_birthdate_statement(text, match.group(1)):
                changed = _append_profile_tag(
                    tags,
                    category="出生日期",
                    value=match.group(1),
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed
                break

        if _looks_like_negative_health_statement(text):
            changed = _append_profile_tag(
                tags,
                category="健康风险/禁忌",
                value="无风险禁忌",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
        else:
            for value, keywords in _HEALTH_TAG_HINTS:
                if any(keyword in text for keyword in keywords) and _looks_like_profile_tag_statement("健康风险/禁忌", value, text):
                    changed = _append_profile_tag(
                        tags,
                        category="健康风险/禁忌",
                        value=value,
                        evidence=evidence,
                        weight_by_category=weight_by_category,
                    ) or changed

        for value, keywords in _TREATMENT_HISTORY_HINTS:
            if any(keyword in text for keyword in keywords) and _looks_like_profile_tag_statement("治疗项目", value, text):
                changed = _append_profile_tag(
                    tags,
                    category="治疗项目",
                    value=value,
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed
        for keyword in _HISTORY_DEVICE_HINTS:
            history_device_source = (
                (text, evidence)
                if keyword in text and _looks_like_profile_tag_statement("历史用的设备/原材料名称", keyword, text)
                else _supported_history_device_source(segments, index, keyword=keyword)
            )
            if history_device_source:
                _, history_device_evidence = history_device_source
                changed = _append_history_device_profile_tags(
                    tags,
                    value=keyword,
                    evidence=history_device_evidence,
                    weight_by_category=weight_by_category,
                ) or changed

        if _looks_like_local_city_statement(text, "外地"):
            changed = _append_profile_tag(
                tags,
                category="常驻城市",
                value="外地",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
        elif _looks_like_local_city_statement(text, "本地"):
            changed = _append_profile_tag(
                tags,
                category="常驻城市",
                value="本地",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        for value, keywords in _PRICE_SENSITIVITY_HINTS:
            if any(keyword in text for keyword in keywords) and _looks_like_price_sensitivity_statement(value, text):
                changed = _append_profile_tag(
                    tags,
                    category="价格敏感度",
                    value=value,
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed

        for value, keywords in _SPECIAL_IDENTITY_HINTS:
            if any(keyword in text for keyword in keywords) and _looks_like_special_identity_statement(value, text):
                changed = _append_profile_tag(
                    tags,
                    category="特殊身份",
                    value=value,
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed

        if _looks_like_personal_status_statement(text, "已婚"):
            changed = _append_profile_tag(
                tags,
                category="个人情况",
                value="已婚",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
            if _text_implies_family_decision(text):
                changed = _append_profile_tag(
                    tags,
                    category="决策主体",
                    value="伴侣",
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed
        elif _looks_like_personal_status_statement(text, "有恋人"):
            changed = _append_profile_tag(
                tags,
                category="个人情况",
                value="有恋人",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
            if _text_implies_family_decision(text):
                changed = _append_profile_tag(
                    tags,
                    category="决策主体",
                    value="伴侣",
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed
        elif _looks_like_personal_status_statement(text, "单身"):
            changed = _append_profile_tag(
                tags,
                category="个人情况",
                value="单身",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        if _text_implies_family_decision(text) and any(
            keyword in text for keyword in ("老公", "丈夫", "老婆", "妻子", "男朋友", "女朋友", "对象", "恋人")
        ):
            changed = _append_profile_tag(
                tags,
                category="决策主体",
                value="伴侣",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        if _text_implies_family_decision(text) and any(keyword in text for keyword in ("妈妈", "母亲", "爸爸", "父母")):
            changed = _append_profile_tag(
                tags,
                category="决策主体",
                value="父母",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        if any(keyword in text for keyword in ("自己决定", "我自己定", "我自己做主", "我自己说了算")):
            changed = _append_profile_tag(
                tags,
                category="决策主体",
                value="自主",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

        if any(keyword in text for keyword in ("无孩", "没孩子", "未育")):
            changed = _append_profile_tag(
                tags,
                category="亲属/子女情况",
                value="无孩",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
        elif any(keyword in text for keyword in ("一孩", "一个孩子", "一个娃")):
            changed = _append_profile_tag(
                tags,
                category="亲属/子女情况",
                value="1孩",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed
        elif any(keyword in text for keyword in ("二孩", "两个孩子", "2孩", "2个孩子", "三孩")):
            changed = _append_profile_tag(
                tags,
                category="亲属/子女情况",
                value="2孩及以上",
                evidence=evidence,
                weight_by_category=weight_by_category,
            ) or changed

    existing_pairs = {
        (_clean_text(str(item.get("category") or "")), _clean_text(str(item.get("value") or "")))
        for item in tags
        if isinstance(item, dict)
    }
    if ("治疗项目", "手术类") not in existing_pairs:
        history_cluster_evidence = _infer_surgical_history_cluster_evidence(segments)
        if history_cluster_evidence:
            changed = _append_profile_tag(
                tags,
                category="治疗项目",
                value="手术类",
                evidence=history_cluster_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if ("治疗项目", "注射类") not in existing_pairs:
        injection_history_evidence = _infer_injection_history_cluster_evidence(segments)
        if injection_history_evidence:
            changed = _append_profile_tag(
                tags,
                category="治疗项目",
                value="注射类",
                evidence=injection_history_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if ("治疗项目", "光电类") not in existing_pairs:
        energy_history_evidence = _infer_energy_history_cluster_evidence(segments)
        if energy_history_evidence:
            changed = _append_profile_tag(
                tags,
                category="治疗项目",
                value="光电类",
                evidence=energy_history_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if not any(_clean_text(str(item.get("category") or "")) == "负面项目/设备/原材料" for item in tags if isinstance(item, dict)):
        negative_project = _infer_negative_project_tag(segments)
        if negative_project:
            negative_value, negative_evidence = negative_project
            changed = _append_profile_tag(
                tags,
                category="负面项目/设备/原材料",
                value=negative_value,
                evidence=negative_evidence,
                weight_by_category=weight_by_category,
            ) or changed

    tags, no_prior_changed = _normalize_explicit_no_prior_treatment_profile_tags(
        tags,
        weight_by_category=weight_by_category,
    )
    changed = changed or no_prior_changed

    if not changed:
        return False

    if age:
        customer_profile["age"] = age
    if age_evidence:
        customer_profile["age_evidence"] = age_evidence
    customer_profile["tags"] = tags
    result_dict["customer_profile"] = customer_profile
    return True


def _sanitize_customer_profile_tags(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    customer_profile = _as_dict(result_dict.get("customer_profile"))
    items = [item for item in _as_list(customer_profile.get("tags")) if isinstance(item, dict)]
    segments = _consultation_segments(raw)
    if not segments:
        return False

    weight_by_category = _profile_weight_by_category()
    kept: list[dict[str, Any]] = []
    changed = False

    supported_age = _find_supported_age_evidence(segments)
    if supported_age is not None:
        age, age_evidence = supported_age
        current_age = _normalize_age_value(customer_profile.get("age"))
        current_age_evidence = _clean_text(customer_profile.get("age_evidence"))
        current_age_supported = _extract_supported_age(current_age_evidence) == current_age if current_age else False
        if age != current_age or not current_age_supported or age_evidence != current_age_evidence:
            customer_profile["age"] = age
            customer_profile["age_evidence"] = age_evidence
            changed = True
    else:
        current_age = _normalize_age_value(customer_profile.get("age"))
        current_age_evidence = _clean_text(customer_profile.get("age_evidence"))
        if current_age and current_age_evidence and _extract_supported_age(current_age_evidence) != current_age:
            customer_profile.pop("age", None)
            customer_profile.pop("age_evidence", None)
            changed = True

    for item in items:
        category = canonicalize_profile_tag_category(item.get("category"))
        if category == "治疗历史":
            category = "治疗项目"
        value = canonicalize_profile_tag_value(category, item.get("value"))
        if not category or not value:
            changed = True
            continue
        keywords, patterns = _profile_tag_match_config(category, value)
        evidence = _find_supported_profile_tag_evidence(
            segments,
            category=category,
            value=value,
            existing_evidence=_clean_text(item.get("evidence")),
            keywords=keywords,
            patterns=patterns,
        )
        if evidence is None:
            changed = True
            continue
        if (
            category != _clean_text(item.get("category"))
            or value != _clean_text(item.get("value"))
            or evidence != _clean_text(item.get("evidence"))
        ):
            changed = True
        _append_profile_tag(
            kept,
            category=category,
            value=value,
            evidence=evidence,
            weight_by_category=weight_by_category,
        )

    treatment_keywords = tuple(keyword for _, keywords in _TREATMENT_HISTORY_HINTS for keyword in keywords)
    for index, _segment in enumerate(segments):
        supported = _supported_fact_source(
            segments,
            index,
            keywords=treatment_keywords + _HISTORY_DEVICE_HINTS,
        )
        if supported is None:
            for keyword in _HISTORY_DEVICE_HINTS:
                history_device_source = _supported_history_device_source(segments, index, keyword=keyword)
                if history_device_source:
                    _, history_device_evidence = history_device_source
                    changed = _append_history_device_profile_tags(
                        kept,
                        value=keyword,
                        evidence=history_device_evidence,
                        weight_by_category=weight_by_category,
                    ) or changed
            continue
        text, evidence = supported
        for value, keywords in _TREATMENT_HISTORY_HINTS:
            if any(keyword in text for keyword in keywords) and _looks_like_profile_tag_statement("治疗项目", value, text):
                changed = _append_profile_tag(
                    kept,
                    category="治疗项目",
                    value=value,
                    evidence=evidence,
                    weight_by_category=weight_by_category,
                ) or changed
        for keyword in _HISTORY_DEVICE_HINTS:
            history_device_source = (
                (text, evidence)
                if keyword in text and _looks_like_profile_tag_statement("历史用的设备/原材料名称", keyword, text)
                else _supported_history_device_source(segments, index, keyword=keyword)
            )
            if history_device_source:
                _, history_device_evidence = history_device_source
                changed = _append_history_device_profile_tags(
                    kept,
                    value=keyword,
                    evidence=history_device_evidence,
                    weight_by_category=weight_by_category,
                ) or changed

    existing_pairs = {
        (_clean_text(str(item.get("category") or "")), _clean_text(str(item.get("value") or "")))
        for item in kept
        if isinstance(item, dict)
    }
    if ("治疗项目", "手术类") not in existing_pairs:
        history_cluster_evidence = _infer_surgical_history_cluster_evidence(segments)
        if history_cluster_evidence:
            changed = _append_profile_tag(
                kept,
                category="治疗项目",
                value="手术类",
                evidence=history_cluster_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if ("治疗项目", "注射类") not in existing_pairs:
        injection_history_evidence = _infer_injection_history_cluster_evidence(segments)
        if injection_history_evidence:
            changed = _append_profile_tag(
                kept,
                category="治疗项目",
                value="注射类",
                evidence=injection_history_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if ("治疗项目", "光电类") not in existing_pairs:
        energy_history_evidence = _infer_energy_history_cluster_evidence(segments)
        if energy_history_evidence:
            changed = _append_profile_tag(
                kept,
                category="治疗项目",
                value="光电类",
                evidence=energy_history_evidence,
                weight_by_category=weight_by_category,
            ) or changed
    if not any(_clean_text(str(item.get("category") or "")) == "负面项目/设备/原材料" for item in kept if isinstance(item, dict)):
        negative_project = _infer_negative_project_tag(segments)
        if negative_project:
            negative_value, negative_evidence = negative_project
            changed = _append_profile_tag(
                kept,
                category="负面项目/设备/原材料",
                value=negative_value,
                evidence=negative_evidence,
                weight_by_category=weight_by_category,
            ) or changed

    validated_kept: list[dict[str, Any]] = []
    for item in kept:
        category = canonicalize_profile_tag_category(item.get("category"))
        value = canonicalize_profile_tag_value(category, item.get("value")) if category else None
        evidence = _clean_text(item.get("evidence"))
        if category and value and _profile_tag_evidence_supports_value(category, value, evidence):
            validated_kept.append(item)
            continue
        changed = True
    kept = validated_kept

    kept, no_prior_changed = _normalize_explicit_no_prior_treatment_profile_tags(
        kept,
        weight_by_category=weight_by_category,
    )
    changed = changed or no_prior_changed

    if not changed and len(kept) == len(items):
        return False

    customer_profile["tags"] = kept
    result_dict["customer_profile"] = customer_profile
    return True


def _backfill_empty_standardized_indications(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    standardized_indications = _as_dict(result_dict.setdefault("standardized_indications", {}))
    if _as_list(standardized_indications.get("items")):
        return False
    forced_items = _standardized_indication_items_from_primary_demands(
        _as_dict(result_dict.get("customer_primary_demands")),
    )
    if forced_items:
        standardized_indications["items"] = forced_items
        summary_names = [
            f"{_clean_text(item.get('indication_name'))}（{_clean_text(item.get('body_part_name'))}）"
            for item in forced_items[:6]
        ]
        standardized_indications["summary"] = f"识别出{len(forced_items)}项适应症：" + "；".join(summary_names)
        result_dict["standardized_indications"] = standardized_indications
        _sync_chief_complaint_standardized_indications(result_dict)
        return True
    segments = _consultation_segments(raw)
    if not segments:
        return False
    changed = _backfill_standardized_indications(
        standardized_indications,
        segments=segments,
        primary_demand_payload=_as_dict(result_dict.get("customer_primary_demands")),
        staff_recommendations_payload=_as_dict(result_dict.get("staff_recommendations")),
    )
    if changed:
        result_dict["standardized_indications"] = standardized_indications
    return changed


_SPARSE_MEDICAL_BUSINESS_INTENT_CUES = (
    "想",
    "了解",
    "咨询",
    "看看",
    "看一下",
    "看眼袋",
    "做",
    "治疗",
    "项目",
    "方案",
    "可以通过",
    "可以做",
    "可以打",
    "可以填充",
    "改善",
    "调整",
    "修复",
    "建议",
    "打",
    "注射",
    "体验",
    "开单",
    "买好",
    "购买",
    "核销",
    "报价",
    "价格",
    "费用",
    "医生",
    "面诊",
)
_SPARSE_MEDICAL_BUSINESS_EXTRA_KEYWORDS = (
    "医美",
    "整形",
    "美容",
    "变美",
    "皮肤管理",
    "注射",
    "微创",
    "手术",
    "水光",
    "美白",
    "光子",
    "玻尿酸",
    "肉毒",
    "瘦脸针",
    "除皱针",
    "双眼皮",
    "眼袋",
    "泪沟",
    "鼻基底",
    "鼻子",
    "脱毛",
    "抗衰",
    "填充",
    "疤痕",
    "瘢痕",
    "斑痕",
)
_SPARSE_INDICATION_SUPPLEMENTAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "暗黄": ("脸色黄", "脸黄", "肤色黄", "美白", "亮肤"),
    "干燥": ("水光", "水光类", "水光针", "补水"),
    "面部除皱": ("瘦脸针", "瘦脸", "咬肌", "除皱针", "肉毒"),
    "纹路": ("法令纹", "鱼尾纹", "川字纹", "抬头纹", "颈纹"),
    "鼻综合": ("鼻部", "隆鼻", "山根", "鼻背", "鼻头", "鼻翼", "鼻孔"),
    "双眼皮": ("双眼皮", "单眼皮", "眼型"),
    "眼袋": ("眼袋", "泪沟", "眶隔"),
    "疤痕": ("瘢痕", "斑痕", "疤", "留疤", "手上有一块"),
}
_SPARSE_MAIN_FACT_FALLBACK_NOTE = (
    "低内容量医美业务场景兜底：录音有效内容较少但存在真实医美咨询信号，强制补提1条主诉和1条适应症"
)
_SPARSE_EVIDENCE_EXCLUDED_CONTEXT_CUES = (
    "术后注意",
    "防晒",
    "退款",
    "电子病历",
    "身份证",
    "办理",
    "售后",
    "美团商家",
)
_SPARSE_NON_BUSINESS_NEGATION_CUES = (
    "没有医美项目",
    "无医美项目",
    "不是医美项目",
    "不是来做医美",
    "不咨询医美",
    "不做医美",
)


def _sparse_medical_business_keywords() -> tuple[str, ...]:
    keywords: list[str] = list(_SPARSE_MEDICAL_BUSINESS_EXTRA_KEYWORDS)
    for hint in _INDICATION_HINTS:
        keywords.extend(str(keyword) for keyword in hint.get("keywords", ()) if keyword)
    for _plan_name, _body_part, plan_keywords in _PLAN_HINTS:
        keywords.extend(plan_keywords)
    return tuple(dict.fromkeys(keywords))


def _meaningful_sparse_segment_text(text: str) -> str:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return ""
    for filler in ("嗯", "啊", "哦", "呃", "哈", "呀", "呢", "嘛", "就是", "然后", "这个", "那个"):
        compact = compact.replace(filler, "")
    return compact


def _is_sparse_effective_consultation(segments: list[dict[str, Any]]) -> bool:
    start_index = _find_consultation_start_index(segments)
    main_segments = segments[start_index:]
    meaningful_texts = [
        meaningful
        for segment in main_segments
        if len(meaningful := _meaningful_sparse_segment_text(_clean_text(segment.get("text")))) >= 8
    ]
    customer_meaningful_count = sum(
        1
        for segment in main_segments
        if _is_customer_side_segment(segment)
        and len(_meaningful_sparse_segment_text(_clean_text(segment.get("text")))) >= 8
    )
    total_chars = sum(len(text) for text in meaningful_texts)
    return (
        total_chars <= 1800
        or len(meaningful_texts) <= 18
        or (_customer_segment_ratio(main_segments) < _LOW_PARTICIPATION_THRESHOLD and customer_meaningful_count <= 5)
    )


def _is_sparse_medical_business_scene(segments: list[dict[str, Any]]) -> bool:
    if not segments or not _is_sparse_effective_consultation(segments):
        return False
    start_index = _find_consultation_start_index(segments)
    full_text = " ".join(_clean_text(segment.get("text")) for segment in segments[start_index:])
    if not full_text:
        return False
    compact_text = re.sub(r"\s+", "", full_text)
    if any(cue in compact_text for cue in _SPARSE_NON_BUSINESS_NEGATION_CUES):
        return False
    business_keywords = _sparse_medical_business_keywords()
    has_medical_keyword = _text_contains_any_keyword(full_text, business_keywords)
    has_business_intent = _text_contains_any_keyword(full_text, _SPARSE_MEDICAL_BUSINESS_INTENT_CUES)
    return has_medical_keyword and (has_business_intent or _allows_main_fact_floor(segments))


def _sparse_indication_keywords(hint: dict[str, Any]) -> tuple[str, ...]:
    indication_name = _clean_text(hint.get("indication_name"))
    return tuple(
        dict.fromkeys(
            tuple(hint.get("keywords", ()))
            + _SPARSE_INDICATION_SUPPLEMENTAL_KEYWORDS.get(indication_name, ())
        )
    )


def _sparse_hint_matches_text(hint: dict[str, Any], text: str) -> bool:
    keywords = _sparse_indication_keywords(hint)
    if not _text_contains_any_keyword(text, keywords):
        return False
    excluded_keywords = tuple(hint.get("excluded_keywords", ()))
    return not (excluded_keywords and _text_contains_excluded_keyword(text, excluded_keywords))


def _sparse_fallback_segment_score(segment: dict[str, Any], *, text: str, hint: dict[str, Any]) -> int:
    if any(cue in text for cue in _SPARSE_EVIDENCE_EXCLUDED_CONTEXT_CUES):
        return -1
    if _looks_like_past_treatment_without_current_need(text):
        return -1
    if re.search(r"(?:去年|之前|以前|暑假|上次).{0,18}(?:做了|做过|打过|弄过)", text) and not any(
        cue in text for cue in ("想", "改善", "调整", "修复", "去掉", "去除")
    ):
        return -1
    if "做完" in text and any(cue in text for cue in ("术后", "防晒", "护理", "更干", "脱水")):
        return -1
    if _looks_like_third_party_narrative_statement(text, keywords=_sparse_indication_keywords(hint)):
        return -1
    if _looks_like_staff_product_explanation_or_self_example(text):
        return -1

    score = 0
    is_customer_side = _is_customer_side_segment(segment)
    has_intent = _text_contains_any_keyword(text, _SPARSE_MEDICAL_BUSINESS_INTENT_CUES)
    if is_customer_side and _looks_like_direct_customer_primary_demand_line(text, _sparse_indication_keywords(hint)):
        score += 80
    elif is_customer_side and has_intent:
        score += 65
    elif is_customer_side:
        score += 50
    elif _is_staff_side_segment(segment) and has_intent:
        score += 35
    elif _is_staff_side_segment(segment) and any(cue in text for cue in ("疤痕", "瘢痕", "斑痕", "瑕疵")):
        score += 30
    elif has_intent:
        score += 25
    else:
        return -1

    begin_ms = int(segment.get("begin", 0) or 0)
    if begin_ms <= 5 * 60 * 1000:
        score += 10
    if begin_ms <= 3 * 60 * 1000:
        score += 10
    if any(cue in text for cue in _CONSULTATION_START_CUES):
        score += 10
    if any(cue in text for cue in ("主要是想", "想来", "想了解", "想做", "看能不能", "修复", "去掉", "去除")):
        score += 20
    if is_customer_side and not _looks_like_direct_customer_primary_demand_line(text, _sparse_indication_keywords(hint)):
        if re.search(r"(?:你|您).{0,8}(?:做完|可以|建议|治疗|医生|项目)", text):
            score -= 35
    if len(text) >= 12:
        score += 5
    return score


def _find_sparse_medical_business_candidate(
    segments: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    start_index = _find_consultation_start_index(segments)
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any], str]] = []
    for segment_index, segment in enumerate(segments[start_index:], start=start_index):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        for hint in _INDICATION_HINTS:
            if not _sparse_hint_matches_text(hint, text):
                continue
            score = _sparse_fallback_segment_score(segment, text=text, hint=hint)
            if score < 0:
                continue
            evidence = _segment_evidence(segment)
            if not evidence:
                continue
            candidates.append((score, -segment_index, segment, hint, evidence))
    if not candidates:
        return None
    _score, _negative_index, segment, hint, evidence = max(candidates, key=lambda item: (item[0], item[1]))
    return segment, hint, evidence


def _looks_like_past_treatment_without_current_need(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    past_cues = (
        "前段时间",
        "之前",
        "以前",
        "原来",
        "上次",
        "去年",
        "暑假",
        "一两年前",
        "很久了",
    )
    treatment_actions = (
        "做过",
        "做了",
        "打过",
        "打了",
        "弄过",
        "弄了",
        "注射过",
        "注射了",
        "填过",
        "填了",
        "尝试过",
        "尝试了",
        "做完",
        "打完",
    )
    if not (any(cue in compact for cue in past_cues) and any(action in compact for action in treatment_actions)):
        return False

    current_need_cues = (
        "这次",
        "今天",
        "过来",
        "主要想",
        "想了解",
        "想咨询",
        "想做",
        "想改善",
        "想调整",
        "想修复",
        "看能不能",
        "最不满意",
        "不满意",
        "效果不好",
        "维持时间不长",
        "重新",
        "修复",
        "改善",
        "调整",
        "去掉",
        "去除",
    )
    if any(cue in compact for cue in current_need_cues):
        return False
    return True


def _sparse_fallback_primary_demand(
    *,
    hint: dict[str, Any],
    body_part: str,
    evidence: str,
) -> tuple[str, str]:
    indication_name = _clean_text(hint.get("indication_name"))
    compact = re.sub(r"\s+", "", _clean_text(evidence))

    if "法令纹" in compact:
        return "改善法令纹，希望面部衔接更平整", "面部"
    if indication_name == "松弛下垂":
        return "改善面部松垮老态，希望轮廓更紧致", body_part or "面部"
    if indication_name == "纹路":
        return "改善面部纹路，希望皮肤更平整", body_part or "面部"
    if indication_name == "暗黄":
        return "改善肤色暗黄，希望提亮肤色", body_part or "面部"
    if indication_name == "干燥":
        return "改善皮肤干燥缺水，希望补水提亮", body_part or "面部"
    if indication_name == "脱毛":
        return "咨询脱毛项目", body_part or "身体"
    if indication_name == "面部除皱":
        if any(keyword in compact for keyword in ("瘦脸", "咬肌")):
            return "改善咬肌和脸型，希望脸部线条更自然", body_part or "面部"
        return "咨询除皱注射，希望改善动态纹", body_part or "面部"
    if indication_name == "双眼皮":
        return "咨询双眼皮/眼型调整", body_part or "眼部"
    if indication_name == "眼袋":
        return "改善眼袋问题，希望眼下更平整", body_part or "眼部"
    if indication_name == "鼻综合":
        return "咨询鼻部塑形方案，希望鼻型更协调", body_part or "鼻部"
    if indication_name == "疤痕":
        return "修复疤痕/瘢痕，希望外观更平整", body_part or "面部"
    if indication_name == "痤疮":
        return "改善痘痘痘印，希望肤质更干净", body_part or "面部"
    return f"咨询{indication_name or '医美'}相关改善方案", body_part or _clean_text(hint.get("default_body_part")) or "面部"


def _force_sparse_medical_business_main_facts(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    primary_payload = _as_dict(result_dict.setdefault("customer_primary_demands", {}))
    indication_payload = _as_dict(result_dict.setdefault("standardized_indications", {}))
    has_primary = bool(_as_list(primary_payload.get("items")))
    has_indication = bool(_as_list(indication_payload.get("items")))
    if has_primary and has_indication:
        return False

    segments = _consultation_segments(raw)
    if not _is_sparse_medical_business_scene(segments):
        return False

    candidate = _find_sparse_medical_business_candidate(segments)
    if candidate is None:
        return False
    _segment, hint, evidence = candidate

    body_part_candidates = _candidate_body_parts(evidence, _clean_text(hint.get("default_body_part")))
    matched = None
    for body_part_name in body_part_candidates:
        matched = resolve_indication_reference_item(
            department_name=hint.get("department_name"),
            indication_name=hint["indication_name"],
            body_part_name=body_part_name,
        )
        if matched is not None:
            break
    if matched is None:
        return False

    changed = False
    if not has_primary:
        demand, body_part = _sparse_fallback_primary_demand(
            hint=hint,
            body_part=matched.body_part_name,
            evidence=evidence,
        )
        primary_payload["items"] = [
            {
                "priority": 1,
                "demand": demand,
                "body_part": body_part,
                "evidence": evidence,
            }
        ]
        primary_payload["summary"] = demand
        result_dict["customer_primary_demands"] = primary_payload
        _sync_chief_complaint_primary_demands(result_dict)
        changed = True

    if not has_indication:
        indication_payload["items"] = [
            {
                "department_code": matched.department_code,
                "department_name": matched.department_name,
                "indication_code": matched.indication_code,
                "indication_name": matched.indication_name,
                "body_part_code": matched.body_part_code,
                "body_part_name": matched.body_part_name,
                "evidence": evidence,
            }
        ]
        indication_payload["summary"] = (
            f"识别出1项适应症：{matched.indication_name}（{matched.body_part_name}）"
        )
        result_dict["standardized_indications"] = indication_payload
        _sync_chief_complaint_standardized_indications(result_dict)
        changed = True

    if changed:
        for payload in (primary_payload, indication_payload):
            note = _clean_text(payload.get("inference_note"))
            if _SPARSE_MAIN_FACT_FALLBACK_NOTE not in note:
                payload["inference_note"] = (
                    f"{note}；{_SPARSE_MAIN_FACT_FALLBACK_NOTE}" if note else _SPARSE_MAIN_FACT_FALLBACK_NOTE
                )
    return changed


def sanitize_analysis_result_with_raw(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    """Repair evidence-backed fields using the original transcript.

    This is intentionally deterministic and side-effect free except mutating the
    provided result dict. It lets API responses and one-off maintenance scripts
    apply the same evidence guardrails as a fresh analysis run without calling
    ASR or the LLM again.
    """
    changed = False
    changed = _sanitize_customer_primary_demands(result_dict, raw=raw) or changed
    changed = _backfill_primary_demands_from_plan_context(result_dict, raw=raw) or changed
    changed = _sanitize_customer_primary_demands(result_dict, raw=raw) or changed
    changed = _sanitize_standardized_indications(result_dict, raw=raw) or changed
    first_item_changed = _backfill_first_consultation_item(result_dict, raw=raw)
    changed = first_item_changed or changed
    if first_item_changed:
        changed = _sanitize_customer_primary_demands(result_dict, raw=raw) or changed
        changed = _sanitize_standardized_indications(result_dict, raw=raw) or changed
    changed = _backfill_customer_profile_tags(result_dict, raw=raw) or changed
    changed = _backfill_staff_recommendations(result_dict, raw=raw) or changed
    changed = _sanitize_staff_recommendations(result_dict, raw=raw) or changed
    changed = _backfill_staff_recommendations(result_dict, raw=raw) or changed
    changed = _sanitize_staff_recommendations(result_dict, raw=raw) or changed
    changed = _sync_consultation_result_recommended_plan(result_dict) or changed
    changed = _backfill_empty_standardized_indications(result_dict, raw=raw) or changed
    changed = _sanitize_standardized_indications(result_dict, raw=raw) or changed
    changed = _backfill_primary_demands_from_plan_context(result_dict, raw=raw) or changed
    changed = _backfill_empty_standardized_indications(result_dict, raw=raw) or changed
    changed = _sanitize_standardized_indications(result_dict, raw=raw) or changed
    changed = _sanitize_customer_profile_tags(result_dict, raw=raw) or changed
    changed = _sanitize_consumption_intent(result_dict, raw=raw) or changed
    changed = _sanitize_customer_concerns(result_dict, raw=raw) or changed
    changed = _force_sparse_medical_business_main_facts(result_dict, raw=raw) or changed
    changed = _backfill_empty_customer_primary_demands(result_dict, raw=raw) or changed
    changed = _sync_consultation_result_recommended_plan(result_dict) or changed
    changed = _sync_consultation_result_customer_profile_summary(result_dict) or changed
    return changed


def _backfill_consumption_intent(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    consumption_intent = _as_dict(result_dict.setdefault("consumption_intent", {}))
    budget = _clean_text(consumption_intent.get("budget"))
    decision_factors = _normalize_text_list(consumption_intent.get("decision_factors"))
    evidence_list = _normalize_text_list(consumption_intent.get("evidence"))
    changed = False

    segments = _consultation_segments(raw)
    if not segments:
        return False

    for index, segment in enumerate(segments):
        supported = _supported_fact_source(
            segments,
            index,
            keywords=tuple(keyword for _, keywords in _DECISION_FACTOR_HINTS for keyword in keywords) + ("预算",),
            allow_money=True,
        )
        if supported is None:
            continue
        text, evidence = supported

        if not budget and "预算" in text:
            amount = _extract_money_text(text)
            if amount:
                budget = amount
                if evidence and evidence not in evidence_list:
                    evidence_list.append(evidence)
                changed = True

        for factor, keywords in _DECISION_FACTOR_HINTS:
            if any(keyword in text for keyword in keywords):
                changed = _append_decision_factor(decision_factors, factor) or changed
                if evidence and evidence not in evidence_list:
                    evidence_list.append(evidence)
    if not changed:
        return False

    if budget:
        consumption_intent["budget"] = budget
    consumption_intent["decision_factors"] = decision_factors
    consumption_intent["evidence"] = evidence_list
    result_dict["consumption_intent"] = consumption_intent
    return True


def _sanitize_consumption_intent(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    consumption_intent = _as_dict(result_dict.get("consumption_intent"))
    if not consumption_intent:
        return False
    segments = _consultation_segments(raw)
    if not segments:
        return False

    changed = False
    budget = _clean_text(consumption_intent.get("budget"))
    decision_factors = _normalize_text_list(consumption_intent.get("decision_factors"))
    evidence_list = _normalize_text_list(consumption_intent.get("evidence"))

    kept_evidence: list[str] = []
    if budget:
        budget_evidence = None
        for evidence in evidence_list:
            supported = _find_supported_evidence_from_existing_text(
                segments,
                existing_evidence=evidence,
                keywords=("预算", budget),
                allow_money=True,
            )
            if supported is not None and (_extract_money_text(supported) or "预算" in supported):
                budget_evidence = supported
                break
        if budget_evidence is None:
            budget = ""
            changed = True
        else:
            kept_evidence.append(budget_evidence)

    kept_factors: list[str] = []
    for factor in decision_factors:
        if factor in _CONCERN_CATEGORY_LABELS or _looks_like_subjective_decision_factor(factor):
            changed = True
            continue
        if factor == "家庭决策":
            evidence = _find_supported_evidence_for_keywords(
                segments,
                keywords=_FAMILY_DECISION_RELATION_HINTS + _FAMILY_DECISION_ACTION_HINTS,
            )
            if evidence is None or not _text_implies_family_decision(evidence):
                changed = True
                continue
            _append_decision_factor(kept_factors, factor)
            kept_evidence.append(evidence)
            continue

        hint = next((entry for entry in _DECISION_FACTOR_HINTS if factor == entry[0]), None)
        keywords = hint[1] if hint else (factor,)
        evidence = None
        for existing in evidence_list:
            evidence = _find_supported_evidence_from_existing_text(
                segments,
                existing_evidence=existing,
                keywords=keywords,
            )
            if evidence is not None:
                break
        evidence = evidence or _find_supported_evidence_for_keywords(segments, keywords=keywords)
        if evidence is None:
            changed = True
            continue
        _append_decision_factor(kept_factors, factor)
        kept_evidence.append(evidence)

    if not changed and kept_factors == decision_factors and (not budget or kept_evidence):
        return False

    consumption_intent["budget"] = budget or None
    consumption_intent["decision_factors"] = kept_factors
    consumption_intent["evidence"] = _dedupe_text_list(kept_evidence)
    result_dict["consumption_intent"] = consumption_intent
    return True


def _backfill_customer_concerns(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    concerns = _as_dict(result_dict.setdefault("customer_concerns", {}))
    items = [
        item
        for item in _as_list(concerns.get("items"))
        if isinstance(item, dict)
    ]
    changed = False

    # Backfill only clear customer-side concerns. Do not try to create a
    # "healthy 3-concern picture"; weak template matches create unsupported
    # business conclusions and misleading evidence.
    if len(items) >= 3:
        return False

    segments = _consultation_segments(raw)
    if not segments:
        return False

    concern_keywords = tuple(keyword for _, _, keywords in _CONCERN_HINTS for keyword in keywords)
    for index, segment in enumerate(segments):
        supported = _supported_fact_source(segments, index, keywords=concern_keywords)
        if supported is None:
            continue
        text, evidence = supported
        if _concern_evidence_strength(evidence, segments) < 2:
            continue
        for concern_type, content, keywords in _CONCERN_HINTS:
            if any(keyword in text for keyword in keywords):
                changed = _append_concern(
                    items,
                    concern_type=concern_type,
                    content=content,
                    evidence=evidence,
                ) or changed
        if len(items) >= 3:
            break

    if not changed:
        return False

    concerns["items"] = items[:3]
    if not _clean_text(concerns.get("summary")):
        concerns["summary"] = "；".join(_clean_text(item.get("content")) for item in items[:3])
    result_dict["customer_concerns"] = concerns
    return True


_CONCERN_CUSTOMER_WORRY_CUES = (
    "怕", "担心", "害怕", "会不会", "影响", "太贵", "贵了", "考虑", "再看", "回去",
    "商量", "比一下", "对比", "麻烦", "风险", "副作用", "失败", "不自然", "明显吗",
    "多久", "多长", "保持", "维持", "利息", "分期", "怎么又多", "一次性完成", "不打了", "不做了",
    "那么远", "跑过来", "赶时间", "弄不了",
)

# Concern types whose semantics overlap (used for dedup/merge)
_CONCERN_TYPE_MERGE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("价格类",),
    ("效果类",),
    ("恢复类",),
    ("疼痛类",),
    ("风险类",),
    ("治疗安排类",),
    ("决策类",),
)


def _concern_evidence_strength(evidence: str, segments: list[dict[str, Any]]) -> int:
    """Score concern evidence strength.

    Returns 0 (no real concern signal) / 1 (weak signal) / 2 (clear customer concern).
    """
    text = _clean_text(evidence)
    if not text:
        return 0

    # Try to locate matching segment(s) to find authoritative customer text.
    customer_texts: list[str] = []
    for seg in segments:
        seg_text = _clean_text(seg.get("text"))
        if not seg_text or seg_text not in text:
            continue
        if (
            _is_customer_side_segment(seg)
            or _is_mislabeled_customer_candidate(seg, segments)
            or (_is_badge_owner_segment(seg) and _looks_like_customer_speech_mislabeled_as_badge_owner(seg_text))
        ):
            customer_texts.append(seg_text)
    customer_text = "\n".join(customer_texts)

    # Fallback to whole evidence text if we cannot attribute to a segment
    # (this happens when evidence is reformatted with [time] prefixes)
    fallback_text = "" if customer_texts else text

    # Reassurance / negation patterns indicate the customer/staff is NOT expressing
    # the concern (e.g. "不害怕", "不担心", "我不怕", "没关系") — should not be
    # treated as a concern signal.
    _REASSURANCE_NEGATIONS = ("不担心", "不害怕", "不怕", "不用怕", "没关系",
                              "不影响", "没事", "无所谓", "不在意")
    has_negation = any(neg in (customer_text + fallback_text) for neg in _REASSURANCE_NEGATIONS)

    worry_in_customer = any(cue in customer_text for cue in _CONCERN_CUSTOMER_WORRY_CUES)
    worry_in_fallback = bool(fallback_text) and any(cue in fallback_text for cue in _CONCERN_CUSTOMER_WORRY_CUES)
    has_question_customer = customer_text and any(m in customer_text for m in ("吗", "?", "？"))

    if has_negation and not has_question_customer:
        # Customer explicitly denies the concern — strength 0.
        return 0

    # Strong: explicit worry/hesitation cues in customer speech
    if worry_in_customer:
        return 2
    if customer_texts and all(_is_brief_customer_confirmation(item) for item in customer_texts) and any(
        cue in text for cue in _CONCERN_CUSTOMER_WORRY_CUES
    ):
        return 2
    # Strong: explicit decision/comparison/recheck cues anywhere in evidence
    decision_cues = ("再考虑", "考虑一下", "回去看", "回去商量", "回去想", "再想想", "和别家比", "比一下", "对比", "先了解")
    if any(cue in text for cue in decision_cues):
        return 2
    # Medium: customer asks a relevant question
    if has_question_customer:
        return 1
    # When we cannot pin to a customer segment but evidence text itself contains
    # worry cues, treat as weak signal (1).
    if worry_in_fallback:
        return 1
    # Fallback: questions about pain/effect/duration/risk in the evidence text
    if fallback_text:
        question_concern_cues = ("痛吗", "痛感", "疼吗", "多疼", "效果怎么", "效果好吗",
                                 "管用吗", "有用吗", "副作用", "风险", "失败", "明显吗",
                                 "维持多久", "保持多久", "多久能", "恢复期", "影响工作",
                                 "影响上班", "敏感", "过敏")
        if any(cue in fallback_text for cue in question_concern_cues):
            return 1
    # No clear signal — likely consultant template/explanation
    return 0


_EVIDENCE_HARD_DELIMITERS = "。！？!?；;\n"
_EVIDENCE_SOFT_DELIMITERS = "，,、 "
_EVIDENCE_MAX_CHARS = 70
_EVIDENCE_MAX_SENTENCES = 2


def _shorten_evidence(text: str, keywords: tuple[str, ...] | None = None) -> str:
    """Trim long evidence to a concise excerpt that preserves the time prefix
    and focuses on sentences containing the most relevant cues.
    """
    raw = (text or "").strip()
    if not raw:
        return raw

    # Preserve leading [HH:MM] / [MM:SS] timestamp if present.
    prefix = ""
    body = raw
    if body.startswith("["):
        end = body.find("]")
        if 0 < end < 12:
            prefix = body[: end + 1]
            body = body[end + 1 :].lstrip()

    # If already short, keep as-is.
    if len(body) <= _EVIDENCE_MAX_CHARS:
        return f"{prefix} {body}".strip() if prefix else body

    # Split into sentences. Prefer hard sentence boundaries; fall back to
    # commas / spaces when there is none, so very long uninterrupted speech
    # can still be trimmed.
    def _split(delimiters: str) -> list[str]:
        out: list[str] = []
        cur: list[str] = []
        for ch in body:
            cur.append(ch)
            if ch in delimiters:
                piece = "".join(cur).strip()
                if piece:
                    out.append(piece)
                cur = []
        tail = "".join(cur).strip()
        if tail:
            out.append(tail)
        return out

    sentences = _split(_EVIDENCE_HARD_DELIMITERS)
    if len(sentences) <= 1 or all(len(s) > _EVIDENCE_MAX_CHARS for s in sentences):
        sentences = _split(_EVIDENCE_HARD_DELIMITERS + _EVIDENCE_SOFT_DELIMITERS)
    if not sentences:
        sentences = [body]

    # Score sentences: prefer ones containing keywords or customer worry cues.
    cue_keywords = tuple(keywords or ())
    def score(sentence: str) -> int:
        s = 0
        if cue_keywords and any(kw and kw in sentence for kw in cue_keywords):
            s += 5
        for cue in _CONCERN_CUSTOMER_WORRY_CUES:
            if cue in sentence:
                s += 2
        for marker in ("吗", "?", "？"):
            if marker in sentence:
                s += 1
        return s

    indexed = list(enumerate(sentences))
    # If any sentence scores positive, pick top scoring (preserving original order on ties)
    scored = [(score(s), i, s) for i, s in indexed]
    has_signal = any(sc > 0 for sc, _, _ in scored)
    if has_signal:
        # Sort by score desc, then position asc; pick best ones, then re-sort by position
        scored.sort(key=lambda t: (-t[0], t[1]))
        picked_indices = sorted(t[1] for t in scored[:_EVIDENCE_MAX_SENTENCES])
        chosen = [sentences[i] for i in picked_indices]
    else:
        chosen = sentences[:_EVIDENCE_MAX_SENTENCES]

    excerpt = "".join(chosen).strip()
    # If still too long, accumulate from the highest scoring fragments only.
    if len(excerpt) > _EVIDENCE_MAX_CHARS:
        # Re-pick using max chars budget.
        budget = _EVIDENCE_MAX_CHARS
        ordered = sorted(scored, key=lambda t: (-t[0], t[1])) if has_signal else [(0, i, sentences[i]) for i in range(len(sentences))]
        kept_idx: list[int] = []
        used = 0
        for _sc, idx, sent in ordered:
            if used + len(sent) > budget and kept_idx:
                continue
            kept_idx.append(idx)
            used += len(sent)
            if used >= budget:
                break
        kept_idx.sort()
        excerpt = "".join(sentences[i] for i in kept_idx).strip()
        if len(excerpt) > _EVIDENCE_MAX_CHARS + 10:
            excerpt = excerpt[: _EVIDENCE_MAX_CHARS].rstrip() + "…"
    return f"{prefix} {excerpt}".strip() if prefix else excerpt


def _find_strong_customer_concern_evidence(
    segments: list[dict[str, Any]],
    keywords: tuple[str, ...],
) -> str | None:
    """Pick the customer-side segment that best expresses a real concern.

    Prefers segments containing both a worry cue and one of the concern keywords.
    Returns the timestamped evidence string, or None if no clear customer concern
    can be located.
    """
    best: tuple[int, int, str] | None = None  # (score, position penalty, evidence)
    for idx, seg in enumerate(segments):
        text = _clean_text(seg.get("text"))
        customerish = (
            _is_customer_side_segment(seg)
            or _is_mislabeled_customer_candidate(seg, segments)
            or (_is_badge_owner_segment(seg) and _looks_like_customer_speech_mislabeled_as_badge_owner(text))
        )
        if not customerish:
            continue
        if not text or not any(kw in text for kw in keywords):
            continue
        # Skip negation/reassurance text — customer is denying the concern.
        if any(neg in text for neg in (
            "不担心", "不害怕", "不怕", "不用怕", "没关系", "不影响", "无所谓", "不在意",
        )):
            continue
        score = 0
        if any(cue in text for cue in _CONCERN_CUSTOMER_WORRY_CUES):
            score += 5
        if any(m in text for m in ("吗", "?", "？")):
            score += 1
        if score == 0:
            continue
        evidence = _segment_evidence(seg)
        if not evidence:
            continue
        if best is None or score > best[0]:
            best = (score, idx, evidence)
    return best[2] if best else None


def _sanitize_customer_concerns(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    concerns = _as_dict(result_dict.get("customer_concerns"))
    items = [item for item in _as_list(concerns.get("items")) if isinstance(item, dict)]
    if not items:
        return False
    segments = _consultation_segments(raw)
    if not segments:
        return False

    enriched: list[tuple[int, dict[str, Any]]] = []
    changed = False
    seen_types: set[str] = set()
    for item in items:
        concern_type = _clean_text(item.get("type"))
        content = _clean_text(item.get("content"))
        hint = next((entry for entry in _CONCERN_HINTS if concern_type == entry[0] or content == entry[1]), None)
        if hint is None:
            hint = next(
                (
                    entry
                    for entry in _CONCERN_HINTS
                    if any(keyword in content for keyword in entry[2])
                ),
                None,
            )
        keywords = hint[2] if hint else (content,)
        evidence = _find_supported_evidence_from_existing_text(
            segments,
            existing_evidence=_clean_text(item.get("evidence")),
            keywords=keywords,
        ) or _find_supported_evidence_for_keywords(
            segments,
            keywords=keywords,
        )
        if evidence is None:
            changed = True
            continue
        if _looks_like_third_party_narrative_statement(evidence, keywords=tuple(keywords)):
            changed = True
            continue
        normalized_type = hint[0] if hint else concern_type
        # Compute strength; if weak, attempt to find a stronger customer-side
        # evidence for this concern before giving up.
        strength = _concern_evidence_strength(evidence, segments)
        if strength < 2:
            stronger = _find_strong_customer_concern_evidence(segments, tuple(keywords))
            if stronger is not None:
                evidence = stronger
                strength = _concern_evidence_strength(evidence, segments)
        if strength < 2:
            changed = True
            continue
        # Dedup by concern type after validation, so a weak unsupported item
        # cannot block a later strong item of the same type.
        if normalized_type and normalized_type in seen_types:
            changed = True
            continue
        seen_types.add(normalized_type)
        short_evidence = _shorten_evidence(evidence, keywords=tuple(keywords))
        if short_evidence != evidence:
            changed = True
        new_item: list[dict[str, Any]] = []
        _append_concern(
            new_item,
            concern_type=normalized_type,
            content=hint[1] if hint else content,
            evidence=short_evidence,
        )
        if new_item:
            enriched.append((strength, new_item[0]))

    # Sort by strength desc, then cap at 3
    enriched.sort(key=lambda x: -x[0])
    kept = [item for _, item in enriched[:3]]
    if len(enriched) > 3:
        changed = True

    if not changed and len(kept) == len(items):
        return False

    concerns["items"] = kept
    concerns["summary"] = "；".join(_clean_text(item.get("content")) for item in kept[:3])
    result_dict["customer_concerns"] = concerns

    # Keep consultation_result.deal_factors.concerns aligned with the sanitized list,
    # regenerate the deal_factors.summary so it doesn't enumerate the full template list,
    # and drop decision_factors that semantically duplicate the surviving concerns.
    kept_concern_texts = [
        _clean_text(item.get("content"))
        for item in kept
        if _clean_text(item.get("content"))
    ]
    consultation_result = result_dict.get("consultation_result")
    if isinstance(consultation_result, dict):
        deal_factors = consultation_result.get("deal_factors")
        if isinstance(deal_factors, dict):
            deal_factors["concerns"] = list(kept_concern_texts)
            df_factors = _normalize_text_list(deal_factors.get("decision_factors"))
            if df_factors:
                deal_factors["decision_factors"] = _filter_overlapping_decision_factors(
                    df_factors,
                    concern_texts=kept_concern_texts,
                    evidence_texts=[],
                    loss_reasons=_normalize_text_list(
                        (consultation_result.get("deal_outcome") or {}).get("loss_reasons")
                    ),
                )
            decision_factors = _normalize_text_list(deal_factors.get("decision_factors"))
            summary_parts = []
            budget = _clean_text(deal_factors.get("budget"))
            if budget:
                summary_parts.append(f"预算：{budget}")
            if kept_concern_texts:
                summary_parts.append("客户顾虑：" + "；".join(kept_concern_texts))
            if decision_factors:
                summary_parts.append("其他影响：" + "；".join(decision_factors))
            deal_factors["summary"] = "；".join(summary_parts) if summary_parts else "未识别到明确成交影响因素。"

    consumption_intent = result_dict.get("consumption_intent")
    if isinstance(consumption_intent, dict):
        ci_factors = _normalize_text_list(consumption_intent.get("decision_factors"))
        if ci_factors:
            consumption_intent["decision_factors"] = _filter_overlapping_decision_factors(
                ci_factors,
                concern_texts=kept_concern_texts,
                evidence_texts=[],
                loss_reasons=[],
            )
    return True


def _infer_acceptance_from_segments(segments: list[dict[str, Any]]) -> str:
    relevant_keywords = _DEAL_SUCCESS_HINTS + _DEAL_PENDING_HINTS + _DEAL_PRICE_LOSS_HINTS
    texts: list[str] = []
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if _is_customer_side_segment(segment) or _is_mislabeled_customer_candidate(segment, segments):
            texts.append(text)
            continue
        if _confirmed_staff_segment_evidence(segments, index, keywords=relevant_keywords):
            texts.append(text)
    combined_text = "\n".join(texts)
    if any(keyword in combined_text for keyword in _DEAL_SUCCESS_HINTS):
        return "接受"
    if any(keyword in combined_text for keyword in _DEAL_PENDING_HINTS + _DEAL_PRICE_LOSS_HINTS):
        return "犹豫"
    return "未明确回应"


def _recommendation_keywords_for_item(item: dict[str, Any]) -> tuple[str, ...]:
    text = " ".join(
        part
        for part in (
            _clean_text(item.get("recommendation")),
            _clean_text(item.get("product_or_solution")),
            _clean_text(item.get("brand")),
            _clean_text(item.get("material")),
            _clean_text(item.get("dosage")),
            _clean_text(item.get("price")),
            _clean_text(item.get("course_or_frequency")),
            _join_recommendation_text_values(item.get("treatment_steps")),
            _clean_text(item.get("implementation_notes")),
        )
        if part
    )
    keywords: list[str] = []
    for plan_name, body_part, plan_keywords in _PLAN_HINTS:
        if (
            plan_name in text
            or any(keyword in text for keyword in plan_keywords)
        ):
            keywords.extend(plan_keywords)
            keywords.append(plan_name)
    for token in re.split(r"[；，。、“”‘’（）()：:、,.\s/]+", text):
        normalized = _clean_text(token)
        if len(normalized) >= 2:
            keywords.append(normalized)
    deduped: list[str] = []
    for keyword in keywords:
        normalized = _clean_text(keyword)
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return tuple(deduped)


def _known_plan_names() -> set[str]:
    return {_clean_text(plan_name) for plan_name, _body_part, _keywords in _PLAN_HINTS}


def _naturalize_staff_recommendation(plan: str, *, body_part: str | None, evidence: str) -> str:
    normalized = _clean_text(plan)
    evidence_text = _clean_text(evidence)
    context = " ".join(part for part in (normalized, _clean_text(body_part), evidence_text) if part)
    compact = re.sub(r"\s+", "", context)
    if not normalized:
        return ""

    if any(keyword in context for keyword in _LIP_CONTEXT_HINTS):
        if any(keyword in compact for keyword in ("溶解", "溶掉", "残留")) and any(
            keyword in compact for keyword in ("少打", "少量", "增加一点", "克制")
        ):
            return "先溶解唇部残留，再克制少量补打"
        if any(keyword in compact for keyword in ("溶解", "溶掉", "残留")):
            return "先处理唇部残留，再评估唇形调整"
        if any(keyword in compact for keyword in ("少打", "少量", "增加一点", "克制")):
            return "唇部少量补打，保持自然克制"

    if any(keyword in context for keyword in _EYE_CONTEXT_HINTS):
        if "泪沟" in compact and any(keyword in compact for keyword in ("胶原", "胶原蛋白", "胶原针")):
            if any(keyword in compact for keyword in ("半年", "再过", "等一等", "消一消", "后面", "以后")):
                return "半年后可考虑胶原/胶原蛋白改善泪沟"
            return "胶原/胶原蛋白改善泪沟"
        if "提眉" in compact and "双眼皮" in compact:
            return "提眉联合双眼皮改善眼部老态"
        if any(keyword in compact for keyword in ("打针", "波波", "玻尿酸", "胶原")):
            return "眼周针剂改善眼袋/凹陷"
        if any(keyword in compact for keyword in ("真性眼袋", "内路", "内切")):
            return "真性眼袋建议内路手术处理"
        if "眼袋" in compact and "手术" in compact:
            return "眼袋建议手术方式处理"

    if any(keyword in compact for keyword in ("超声炮", "超声刀", "热玛吉", "热拉提", "射频")):
        if "超声炮" in compact and "打针" in compact:
            return "超声炮联合针剂提升紧致"
        if "4999" in compact or "二代" in compact or "上一版本" in compact:
            return "4999二代超声炮全脸提升紧致"
        return "超声/射频类抗衰提升紧致"

    if any(keyword in context for keyword in _NOSE_CONTEXT_HINTS):
        if any(keyword in compact for keyword in ("鼻小柱", "鼻头", "鼻尖", "鼻基底", "鼻坎基底")) and any(
            keyword in compact for keyword in ("定彩", "瑞德喜", "芭比", "再生", "玻尿酸", "支撑")
        ):
            return "鼻小柱/鼻头/鼻基底支撑型注射调整"
        if any(keyword in compact for keyword in ("玻尿酸", "填充")):
            return "鼻部局部玻尿酸支撑调整"
        if any(keyword in compact for keyword in ("鼻综合", "手术", "膨体", "假体")):
            return "鼻部手术方案调整整体鼻型"

    if any(keyword in compact for keyword in ("后背", "背部", "小后背", "大后背", "吸脂", "抽脂", "超脂", "超脂术", "富贵包")):
        if "小后背" in compact and "大后背" in compact:
            return "按小后背收费，实际处理大后背并加超脂术"
        if "富贵包" in compact:
            return "后背/富贵包超脂术塑形"
        return "后背吸脂/超脂术塑形"

    return normalized


def _recommendation_text_is_negated_alternative(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    negated_cues = (
        "暂不",
        "不再",
        "别再",
        "不能",
        "不用",
        "不要",
        "避免",
        "不建议",
        "先别",
    )
    return any(cue in compact for cue in negated_cues)


def _recommendation_evidence_has_positive_alternative(evidence: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(evidence))
    if not compact:
        return False
    positive_action_cues = (
        "可以",
        "能",
        "建议",
        "适合",
        "打点",
        "打一",
        "打胶原",
        "填充",
        "做",
    )
    positive_material_cues = (
        "胶原",
        "胶原蛋白",
        "胶原针",
        "肉毒",
        "除皱",
        "水光",
        "光子",
        "超声炮",
        "热玛吉",
        "手术",
        "内路",
        "内切",
        "眶隔",
        "脂肪",
    )
    return any(cue in compact for cue in positive_action_cues) and any(cue in compact for cue in positive_material_cues)


def _should_naturalize_recommendation_text(recommendation: str, product_or_solution: str) -> bool:
    known_plan_names = _known_plan_names()
    context = f"{recommendation} {product_or_solution}"
    return (
        recommendation in known_plan_names
        or product_or_solution in known_plan_names
        or recommendation == product_or_solution
        or any(keyword in context for keyword in ("超声炮", "超声刀", "热玛吉", "热拉提", "射频", "超声抗衰"))
        or _recommendation_text_is_negated_alternative(context)
    )


def _recommendation_context_text(item: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean_text(item.get("recommendation")),
            _clean_text(item.get("product_or_solution")),
            _clean_text(item.get("body_part")),
            _clean_text(item.get("brand")),
            _clean_text(item.get("material")),
            _clean_text(item.get("dosage")),
            _clean_text(item.get("price")),
            _clean_text(item.get("course_or_frequency")),
            _join_recommendation_text_values(item.get("treatment_steps")),
            _clean_text(item.get("implementation_notes")),
            _clean_text(item.get("evidence")),
        )
        if part
    )


def _join_recommendation_text_values(value: object) -> str:
    if isinstance(value, list):
        return "；".join(_clean_text(item) for item in value if _clean_text(item))
    if isinstance(value, tuple):
        return "；".join(_clean_text(item) for item in value if _clean_text(item))
    return _clean_text(value)


_RECOMMENDATION_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("brand", "品牌"),
    ("material", "材料"),
    ("dosage", "用量"),
    ("price", "报价"),
    ("course_or_frequency", "疗程"),
    ("treatment_steps", "步骤"),
    ("implementation_notes", "要点"),
)


def _format_recommendation_plan_text(item: dict[str, Any]) -> str:
    plan = _clean_text(item.get("recommendation")) or _clean_text(item.get("product_or_solution"))
    if not plan:
        return ""
    compact_plan = re.sub(r"\s+", "", plan)
    details: list[str] = []
    seen_values: set[str] = set()
    for field, label in _RECOMMENDATION_DETAIL_FIELDS:
        value = _join_recommendation_text_values(item.get(field))
        if not value:
            continue
        compact_value = re.sub(r"\s+", "", value)
        if not compact_value or compact_value in compact_plan or compact_value in seen_values:
            continue
        seen_values.add(compact_value)
        details.append(f"{label}：{value}")
    if not details:
        return plan
    return f"{plan}（{'；'.join(details)}）"


def _recommendation_evidence_time_marks(text: str) -> set[str]:
    return set(re.findall(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", _clean_text(text)))


def _recommendation_duplicate_signals(text: str) -> set[str]:
    normalized = _clean_text(text)
    if not normalized:
        return set()
    signal_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("tear_trough", ("泪沟", "眼下凹陷")),
        ("nasal_base", ("鼻基底", "鼻翼基底", "鼻坎基底", "鼻底")),
        ("collagen", ("胶原", "胶原蛋白", "胶原针")),
        ("filler", ("填充", "注射", "补性材料", "不含玻尿酸")),
        ("radiesse", ("瑞德喜", "德国的瑞典", "德国瑞典")),
        ("shuangmei", ("双美", "双美胶原蛋白")),
    )
    return {
        signal
        for signal, keywords in signal_groups
        if any(keyword in normalized for keyword in keywords)
    }


def _recommendation_items_look_duplicate(item: dict[str, Any], kept_item: dict[str, Any]) -> bool:
    item_evidence = _clean_text(item.get("evidence"))
    kept_evidence = _clean_text(kept_item.get("evidence"))
    if not item_evidence or not kept_evidence:
        return False

    item_context = _recommendation_context_text(item)
    kept_context = _recommendation_context_text(kept_item)
    body_overlaps = bool(_context_body_families(item_context) & _context_body_families(kept_context))
    same_body = _clean_text(item.get("body_part")) and _clean_text(item.get("body_part")) == _clean_text(kept_item.get("body_part"))
    same_family = _recommendation_family(item_context) and _recommendation_family(item_context) == _recommendation_family(kept_context)

    if item_evidence == kept_evidence:
        return same_body or body_overlaps or same_family

    nested_evidence = item_evidence in kept_evidence or kept_evidence in item_evidence
    time_overlaps = bool(
        _recommendation_evidence_time_marks(item_evidence)
        & _recommendation_evidence_time_marks(kept_evidence)
    )
    if not nested_evidence and not time_overlaps:
        return False

    shared_signals = _recommendation_duplicate_signals(item_context) & _recommendation_duplicate_signals(kept_context)
    return bool(same_family or (body_overlaps and len(shared_signals) >= 2))


def _recommendation_brand_choices_from_evidence(evidence: str) -> tuple[str, ...]:
    text = _clean_text(evidence)
    if not text:
        return ()
    choices: list[str] = []
    if "双美胶原蛋白" in text or ("双美" in text and "胶原" in text):
        choices.append("双美胶原蛋白")
    if (
        "瑞德喜" in text
        or "德国的瑞典" in text
        or "德国瑞典" in text
        or ("瑞典" in text and any(keyword in text for keyword in ("两个选择", "两种选择", "补性材料", "不含玻尿酸")))
    ):
        choices.append("瑞德喜")

    deduped: list[str] = []
    for choice in choices:
        if choice not in deduped:
            deduped.append(choice)
    return tuple(deduped)


def _merge_recommendation_brand_values(existing: str, choices: tuple[str, ...]) -> str:
    values: list[str] = []
    for choice in choices:
        if choice not in values:
            values.append(choice)
    for part in re.split(r"[、/或,，；;\s]+", existing):
        normalized = _clean_text(part)
        if normalized and normalized not in values:
            values.append(normalized)
    return "或".join(values)


def _enrich_recommendation_item_from_evidence(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    evidence = _clean_text(item.get("evidence"))
    if not evidence:
        return item, False

    changed = False
    updated = item
    brand_choices = _recommendation_brand_choices_from_evidence(evidence)
    if brand_choices:
        brand = _clean_text(updated.get("brand"))
        merged_brand = _merge_recommendation_brand_values(brand, brand_choices)
        if merged_brand and merged_brand != brand:
            updated = {**updated, "brand": merged_brand}
            changed = True

    if brand_choices and "不含玻尿酸" in evidence and "补性材料" in evidence:
        material = _clean_text(updated.get("material"))
        target_material = "不含玻尿酸的补性材料"
        if not material or material in {"再生类材料", "胶原/胶原蛋白", "胶原蛋白"}:
            updated = {**updated, "material": target_material}
            changed = True

    recommendation = _clean_text(updated.get("recommendation"))
    if (
        recommendation
        and len(brand_choices) >= 2
        and any(keyword in evidence for keyword in ("两个选择", "两种选择", "可选", "一是", "二是"))
        and not all(choice in recommendation for choice in brand_choices)
    ):
        enriched_recommendation = recommendation
        for choice in brand_choices:
            enriched_recommendation = enriched_recommendation.replace(choice, "不含玻尿酸的补性材料")
        enriched_recommendation = re.sub(
            r"不含玻尿酸的补性材料(的)?补性材料",
            "不含玻尿酸的补性材料",
            enriched_recommendation,
        )
        enriched_recommendation = re.sub(
            r"(使用一支|使用)(不含玻尿酸的补性材料)",
            r"\1\2",
            enriched_recommendation,
        )
        if "可选" not in enriched_recommendation:
            enriched_recommendation = f"{enriched_recommendation}，可选{'或'.join(brand_choices)}"
        if enriched_recommendation != recommendation:
            updated = {**updated, "recommendation": enriched_recommendation}
            product = _clean_text(updated.get("product_or_solution"))
            if product == recommendation:
                updated["product_or_solution"] = enriched_recommendation
            changed = True

    return updated, changed


def _recommendation_matches_primary_demand_item(item: dict[str, Any], primary_item: dict[str, Any]) -> bool:
    rec_context = _recommendation_context_text(item)
    primary_context = _primary_demand_item_context(primary_item)
    if not rec_context or not primary_context:
        return False

    rec_body = _clean_text(item.get("body_part"))
    primary_body = _clean_text(primary_item.get("body_part"))
    if rec_body and primary_body and (rec_body in primary_body or primary_body in rec_body):
        return True

    if _context_contains_any(rec_context, _LIP_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _LIP_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _EYE_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _EYE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NOSE_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _NOSE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _CHEST_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _CHEST_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NECK_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _NECK_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _BODY_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _BODY_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _FACE_CONTEXT_HINTS):
        return _context_contains_any(primary_context, _FACE_CONTEXT_HINTS)
    return False


def _matching_primary_demand_for_recommendation(item: dict[str, Any], primary_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for primary_item in primary_items:
        if _recommendation_matches_primary_demand_item(item, primary_item):
            return primary_item
    return None


def _recommendation_matches_indication_item(item: dict[str, Any], indication_item: dict[str, Any]) -> bool:
    rec_context = _recommendation_context_text(item)
    indication_context = " ".join(
        part
        for part in (
            _clean_text(indication_item.get("indication_name")),
            _clean_text(indication_item.get("body_part_name")),
            _clean_text(indication_item.get("evidence")),
        )
        if part
    )
    if not rec_context or not indication_context:
        return False
    if _context_contains_any(rec_context, _LIP_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _LIP_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _EYE_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _EYE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NOSE_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _NOSE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _CHEST_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _CHEST_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NECK_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _NECK_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _BODY_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _BODY_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _FACE_CONTEXT_HINTS):
        return _context_contains_any(indication_context, _FACE_CONTEXT_HINTS)
    return False


def _recommendation_body_supported_by_evidence(item: dict[str, Any], evidence: str) -> bool:
    rec_context = _recommendation_context_text(item)
    evidence_text = _clean_text(evidence)
    if not rec_context or not evidence_text:
        return False
    if _context_contains_any(rec_context, _LIP_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _LIP_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _EYE_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _EYE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NOSE_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _NOSE_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _CHEST_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _CHEST_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _NECK_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _NECK_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _BODY_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _BODY_CONTEXT_HINTS)
    if _context_contains_any(rec_context, _FACE_CONTEXT_HINTS):
        return _context_contains_any(evidence_text, _FACE_CONTEXT_HINTS)
    return False


def _recommendation_has_standalone_strong_support(item: dict[str, Any], evidence: str) -> bool:
    evidence_text = _clean_text(evidence)
    if not evidence_text:
        return False
    if not _recommendation_body_supported_by_evidence(item, evidence_text):
        return False

    issue_cues = (
        "你有",
        "你这",
        "你的",
        "这个位置",
        "这个地方",
        "真性",
        "假性",
        "残留",
        "不满意",
        "效果不好",
        "低",
        "塌",
        "凹",
        "松",
        "垂",
        "肥",
        "宽",
        "厚",
        "大",
        "短",
        "疤",
        "眼袋",
        "泪沟",
        "咬肌",
        "法令纹",
    )
    action_cues = _RECOMMENDATION_EVIDENCE_CUES + (
        "做手术",
        "手术",
        "内路",
        "内切",
        "外切",
        "眶隔",
        "填充",
        "注射",
        "打一点",
        "打点",
    )
    return any(cue in evidence_text for cue in issue_cues) and any(cue in evidence_text for cue in action_cues)


def _context_body_families(text: str) -> set[str]:
    families: set[str] = set()
    normalized = _clean_text(text)
    if not normalized:
        return families
    checks = (
        ("lip", _LIP_CONTEXT_HINTS),
        ("eye", _EYE_CONTEXT_HINTS),
        ("nose", _NOSE_CONTEXT_HINTS),
        ("chest", _CHEST_CONTEXT_HINTS),
        ("neck", _NECK_CONTEXT_HINTS),
        ("body", _BODY_CONTEXT_HINTS),
        ("face", _FACE_CONTEXT_HINTS),
    )
    for family, keywords in checks:
        if _context_contains_any(normalized, keywords):
            families.add(family)
    return families


def _recommendation_conflicts_with_primary_scope(
    item: dict[str, Any],
    evidence: str,
    *,
    primary_items: list[dict[str, Any]],
    indication_items: list[dict[str, Any]],
) -> bool:
    rec_families = _context_body_families(_recommendation_context_text({**item, "evidence": evidence}))
    rec_specific = rec_families - {"face"}
    if not rec_specific:
        return False

    target_text_parts: list[str] = []
    for primary_item in primary_items:
        target_text_parts.append(_primary_demand_item_context(primary_item))
    for indication_item in indication_items:
        target_text_parts.append(
            " ".join(
                part
                for part in (
                    _clean_text(indication_item.get("indication_name")),
                    _clean_text(indication_item.get("body_part_name")),
                    _clean_text(indication_item.get("evidence")),
                )
                if part
            )
        )
    target_families = _context_body_families(" ".join(target_text_parts))
    target_specific = target_families - {"face"}
    if not target_specific:
        return False
    return rec_specific.isdisjoint(target_specific)


def _recommendation_specific_tokens(text: str) -> tuple[str, ...]:
    normalized = _clean_text(text)
    tokens: list[str] = []
    for token in (
        "斐然",
        "乔雅登",
        "润致",
        "海薇",
        "艾莉薇",
        "伊婉",
        "濡白天使",
        "少女",
        "薇旖美",
        "菲林普利",
        "贝丽菲尔",
        "贝利菲尔",
        "菲利菲尔",
    ):
        if token in normalized:
            tokens.append(token)
    if re.search(r"\b\d+(?:\.\d+)?\s*ml\b|[一二两三四五六七八九十]+毫升", normalized, flags=re.IGNORECASE):
        tokens.append("剂量")
    return tuple(tokens)


def _looks_like_negated_filler_non_recommendation(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    negated_cues = (
        "没说玻尿酸不能打",
        "不说玻尿酸不能打",
        "不是说玻尿酸不能打",
        "没说不能打",
        "不是不能打",
        "我没说不让你打",
        "不要瞎打",
        "不要打过量",
    )
    if not any(cue in compact for cue in negated_cues):
        return False
    concrete_plan_cues = (
        "建议",
        "推荐",
        "可以做",
        "可以打",
        "可以填",
        "给你打",
        "给你做",
        "配合",
        "结合",
        "至少",
        "支的量",
        "方案",
        "报价",
    )
    return not any(cue in compact for cue in concrete_plan_cues)


def _recommendation_evidence_supports_item(
    item: dict[str, Any],
    evidence: str,
    *,
    primary_items: list[dict[str, Any]],
    indication_items: list[dict[str, Any]],
) -> bool:
    evidence_text = _clean_text(evidence)
    if not evidence_text:
        return False
    keywords = _recommendation_keywords_for_item(item)
    if not _staff_segment_looks_like_recommendation(evidence_text, keywords):
        return False

    evidence_item = {**item, "evidence": evidence}
    matches_primary = _matching_primary_demand_for_recommendation(evidence_item, primary_items) is not None
    matches_indication = any(_recommendation_matches_indication_item(evidence_item, indication) for indication in indication_items)
    standalone_supported = _recommendation_has_standalone_strong_support(evidence_item, evidence)
    if standalone_supported and not (matches_primary or matches_indication):
        if _recommendation_conflicts_with_primary_scope(
            evidence_item,
            evidence,
            primary_items=primary_items,
            indication_items=indication_items,
        ):
            return False
    if not (matches_primary or matches_indication or standalone_supported):
        return False

    item_specific_tokens = _recommendation_specific_tokens(
        " ".join(
            part
            for part in (
                _clean_text(item.get("recommendation")),
                _clean_text(item.get("product_or_solution")),
            )
            if part
        )
    )
    if item_specific_tokens:
        if "剂量" in item_specific_tokens and not re.search(r"\b\d+(?:\.\d+)?\s*ml\b|[一二两三四五六七八九十]+毫升", evidence_text, flags=re.IGNORECASE):
            return False
        concrete_tokens = tuple(token for token in item_specific_tokens if token != "剂量")
        if concrete_tokens and not any(token in evidence_text for token in concrete_tokens):
            return False
    return True


def _staff_segment_looks_like_recommendation(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = _clean_text(text)
    if not normalized or not keywords:
        return False
    compact = re.sub(r"\s+", "", normalized)
    if _looks_like_negated_filler_non_recommendation(compact):
        return False
    if (
        any(cue in compact for cue in ("融了玻尿酸", "溶了玻尿酸", "玻尿酸是融了", "胶原是没有溶掉", "胶原没有溶掉"))
        and not any(cue in compact for cue in ("建议", "推荐", "先打", "可以打", "少量", "补打", "增加一点", "填充", "方案"))
    ):
        return False
    if not any(keyword in normalized for keyword in keywords):
        return False
    if not _plan_keywords_have_recommendation_context(normalized, keywords):
        return False
    if _looks_like_staff_demo_or_example_statement(normalized):
        return False
    if _looks_like_treatment_history_statement(normalized) and not any(
        cue in normalized for cue in _RECOMMENDATION_EVIDENCE_CUES
    ):
        return False
    return any(cue in normalized for cue in _RECOMMENDATION_EVIDENCE_CUES) or bool(
        re.search(r"(?:用|采用).{0,20}(?:填充|注射).{0,20}(?:改善|调整|处理)", compact)
        or re.search(r"(?:填充|注射).{0,20}(?:可以改善|改善|调整|处理)", compact)
    )


def _plan_keywords_have_recommendation_context(text: str, keywords: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    recommendation_context_cues = (
        "建议",
        "推荐",
        "适合",
        "可以",
        "能",
        "方案",
        "选择",
        "联合",
        "配合",
        "设计",
        "安排",
        "可以配",
        "配两支",
        "配一支",
        "一起做",
        "你就买",
        "买那个",
        "直接买",
        "先买",
        "购买",
        "用",
        "填",
        "填充",
        "注射",
        "打一",
        "打点",
        "打几",
        "要打",
        "少打",
        "少量",
        "增加一点",
        "溶解",
        "溶掉",
        "做一个",
        "做一下",
    )
    history_only_cues = (
        "打过",
        "做过",
        "以前",
        "之前",
        "原来",
        "上次",
        "情况",
        "残留",
        "融了",
        "融了玻尿酸",
        "溶掉",
        "溶了",
        "取出",
        "修复",
        "不满意",
        "效果不好",
    )
    strong_context_cues = (
        "建议",
        "推荐",
        "适合",
        "方案",
        "填充",
        "注射",
        "可以填",
        "用",
        "要打",
        "少打",
        "增加一点",
        "溶解",
        "溶掉",
    )
    negated_material_cues = (
        "不能",
        "别再",
        "可别",
        "不用",
        "不要",
        "不建议",
        "先别",
        "避免",
    )
    for keyword in keywords:
        compact_keyword = re.sub(r"\s+", "", _clean_text(keyword))
        if not compact_keyword:
            continue
        start = compact.find(compact_keyword)
        while start >= 0:
            end = start + len(compact_keyword)
            window = compact[max(0, start - 18) : min(len(compact), end + 18)]
            if any(cue in window for cue in negated_material_cues) and not any(
                re.search(rf"{cue_pattern}.{{0,6}}{re.escape(compact_keyword)}", window)
                for cue_pattern in ("可以", r"(?<!不)能", "建议", "推荐", "适合", r"(?<!不)用来", "填充", "注射")
            ):
                start = compact.find(compact_keyword, end)
                continue
            if any(cue in window for cue in history_only_cues) and not any(
                cue in window for cue in strong_context_cues
            ):
                start = compact.find(compact_keyword, end)
                continue
            if any(cue in window for cue in recommendation_context_cues):
                return True
            start = compact.find(compact_keyword, end)
    return False


_RECOMMENDATION_CUSTOMER_CONTEXT_CUES = (
    "适合我",
    "适合",
    "多少钱",
    "价格",
    "实惠",
    "性价比",
    "效果",
    "能承受",
    "承受",
    "花不了",
    "拿不出来",
    "想要",
    "想改善",
    "我这个",
)
_RECOMMENDATION_CUSTOMER_RESPONSE_CUES = (
    "可以接受",
    "能达到",
    "效果",
    "满意",
    "先放一放",
    "没考虑",
    "除了这个",
    "暂时不",
    "不打",
    "不做",
    "会不会",
    "见不得人",
    "显得丑",
    "创伤",
    "提了眉",
    "价格",
    "多少钱",
    "性价比",
    "承受",
)


def _segment_begin_ms(segment: dict[str, Any]) -> int:
    return int(segment.get("begin") or segment.get("begin_ms") or 0)


def _first_evidence_timestamp_ms(evidence: str) -> int | None:
    match = re.search(r"\[(\d{2}):(\d{2})\]", _clean_text(evidence))
    if not match:
        return None
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    return (minutes * 60 + seconds) * 1000


def _recommendation_context_evidence(
    segments: list[dict[str, Any]],
    index: int,
    *,
    keywords: tuple[str, ...] = (),
) -> str:
    current = segments[index] if 0 <= index < len(segments) else {}
    current_evidence = _segment_evidence(current)
    if not current_evidence:
        return ""

    current_begin = _segment_begin_ms(current)
    parts: list[str] = []
    for prev_index in range(max(0, index - 4), index):
        previous = segments[prev_index]
        previous_text = _clean_text(previous.get("text"))
        if not previous_text or current_begin - _segment_begin_ms(previous) > 60_000:
            continue
        if _is_staff_side_segment(previous) and any(cue in previous_text for cue in ("结合针剂", "至少", "支的量", "四支", "一支", "凹")) and any(
            cue in previous_text for cue in ("你这个", "这个地方", "针剂", "凹")
        ):
            evidence = _segment_evidence(previous)
            if evidence:
                parts.append(evidence)
            continue
        if not (_is_customer_side_segment(previous) or _is_mislabeled_customer_candidate(previous, segments)):
            continue
        if not (
            any(cue in previous_text for cue in _RECOMMENDATION_CUSTOMER_CONTEXT_CUES)
            or any(keyword and keyword in previous_text for keyword in keywords)
        ):
            continue
        evidence = _segment_evidence(previous)
        if evidence:
            parts.append(evidence)
    parts = parts[-2:]
    parts.append(current_evidence)

    for next_index in range(index + 1, min(len(segments), index + 4)):
        following = segments[next_index]
        following_text = _clean_text(following.get("text"))
        if not following_text or _segment_begin_ms(following) - current_begin > 45_000:
            break
        if _is_staff_side_segment(following) and any(keyword and keyword in following_text for keyword in keywords):
            evidence = _segment_evidence(following)
            if evidence:
                parts.append(evidence)
            if len(parts) >= 4:
                break
            continue
        if not (_is_customer_side_segment(following) or _is_mislabeled_customer_candidate(following, segments)):
            continue
        if not any(cue in following_text for cue in _RECOMMENDATION_CUSTOMER_RESPONSE_CUES):
            continue
        evidence = _segment_evidence(following)
        if evidence:
            parts.append(evidence)
        if len(parts) >= 4:
            break

    return "\n".join(_dedupe_text_list(parts))


def _find_supported_recommendation_evidence(
    segments: list[dict[str, Any]],
    *,
    existing_evidence: str,
    keywords: tuple[str, ...],
) -> str | None:
    if not keywords:
        return None

    existing_lines = [
        re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
        for raw_text in _normalize_text_list(existing_evidence)
        for raw_line in raw_text.splitlines()
        if re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
    ]
    if existing_lines and _staff_segment_looks_like_recommendation("\n".join(existing_lines), keywords):
        for raw_line in existing_lines:
            for segment_index, segment in enumerate(segments):
                text = _clean_text(segment.get("text"))
                if text and raw_line in text and _staff_segment_looks_like_recommendation(text, keywords):
                    return _recommendation_context_evidence(segments, segment_index, keywords=keywords)
        timestamp_ms = _first_evidence_timestamp_ms(existing_evidence)
        if timestamp_ms is not None:
            for segment_index, segment in enumerate(segments):
                if abs(_segment_begin_ms(segment) - timestamp_ms) > 3_000:
                    continue
                text = _clean_text(segment.get("text"))
                if _staff_segment_looks_like_recommendation(text, keywords):
                    return _recommendation_context_evidence(segments, segment_index, keywords=keywords)
        return existing_evidence

    for raw_text in _normalize_text_list(existing_evidence):
        for raw_line in raw_text.splitlines():
            line = re.sub(r"^\[\d{2}:\d{2}\]\s*", "", raw_line).strip()
            if not line:
                continue
            for segment in segments:
                text = _clean_text(segment.get("text"))
                if not text or line not in text:
                    continue
                if _staff_segment_looks_like_recommendation(text, keywords):
                    return _recommendation_context_evidence(segments, segments.index(segment), keywords=keywords)

    for segment_index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not _staff_segment_looks_like_recommendation(text, keywords):
            continue
        evidence = _recommendation_context_evidence(segments, segment_index, keywords=keywords)
        if evidence:
            return evidence
    return None


def _lip_residual_recommendation_evidence(segments: list[dict[str, Any]], index: int) -> str | None:
    if index >= len(segments):
        return None
    current = segments[index]
    start_ms = int(current.get("begin", 0) or 0)
    parts = [_segment_evidence(current)]
    context_keywords = ("溶解", "溶掉", "增加一点", "少打", "少量", "克制")
    for offset in range(1, 8):
        next_index = index + offset
        if next_index >= len(segments):
            break
        segment = segments[next_index]
        begin_ms = int(segment.get("begin", 0) or 0)
        if begin_ms - start_ms > 75_000:
            break
        text = _clean_text(segment.get("text"))
        if not text or not any(keyword in text for keyword in context_keywords):
            continue
        evidence = _segment_evidence(segment)
        if evidence and evidence not in parts:
            parts.append(evidence)
        if len(parts) >= 3:
            break
    return "\n".join(part for part in parts if part)


def _backfill_staff_recommendations(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    recommendations = _as_dict(result_dict.setdefault("staff_recommendations", {}))
    items = [
        item
        for item in _as_list(recommendations.get("items"))
        if isinstance(item, dict)
    ]
    primary_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
        if isinstance(item, dict)
    ]
    indication_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("standardized_indications")).get("items"))
        if isinstance(item, dict)
    ]
    segments = _consultation_segments(raw)
    if not segments:
        return False

    acceptance = _infer_acceptance_from_segments(segments)
    changed = False
    seen_plans = {
        (
            _recommendation_family(
                " ".join(
                    part
                    for part in (
                        _clean_text(item.get("recommendation")),
                        _clean_text(item.get("product_or_solution")),
                    )
                    if part
                )
            ),
            _clean_text(item.get("body_part")),
        )
        for item in items
    }

    for segment_index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        evidence = _segment_evidence(segment)
        for plan_name, body_part, keywords in _PLAN_HINTS:
            if not any(keyword in text for keyword in keywords):
                continue
            if plan_name == "胶原/胶原蛋白泪沟填充" and not (
                "泪沟" in text and any(keyword in text for keyword in ("胶原", "胶原蛋白", "胶原针"))
            ):
                continue
            if not _staff_segment_looks_like_recommendation(text, keywords + (plan_name,)):
                continue
            key = (_recommendation_family(plan_name), _clean_text(body_part))
            if key in seen_plans:
                continue
            body_part_value = body_part
            demand_priority = _infer_demand_priority(primary_items, body_part=body_part, text=text)
            evidence = _recommendation_context_evidence(segments, segment_index, keywords=keywords + (plan_name,))
            if plan_name == "唇部残留溶解后少量塑形":
                evidence = _lip_residual_recommendation_evidence(segments, segment_index) or evidence
            display_plan_name = _naturalize_staff_recommendation(
                plan_name,
                body_part=body_part_value,
                evidence=evidence,
            )
            candidate = {
                "recommendation": display_plan_name,
                "product_or_solution": display_plan_name,
                "body_part": body_part_value,
                "evidence": evidence,
                "customer_response": acceptance,
                "demand_priority": demand_priority,
            }
            if not _recommendation_evidence_supports_item(
                candidate,
                evidence,
                primary_items=primary_items,
                indication_items=indication_items,
            ):
                continue
            items.append(
                candidate
            )
            seen_plans.add(key)
            changed = True
            break
        if len(items) >= 3:
            break

    if not changed:
        return False

    recommendations["items"] = items
    if not _clean_text(recommendations.get("summary")):
        recommendations["summary"] = "；".join(
            _format_recommendation_plan_text(item)
            for item in items[:3]
            if _format_recommendation_plan_text(item)
        )
    result_dict["staff_recommendations"] = recommendations
    return True


def _sanitize_staff_recommendations(result_dict: dict[str, Any], *, raw: dict[str, Any] | None = None) -> bool:
    recommendations = _as_dict(result_dict.get("staff_recommendations"))
    items = [item for item in _as_list(recommendations.get("items")) if isinstance(item, dict)]
    if not items:
        return False

    segments = _consultation_segments(raw) if raw else []
    valid_demand_priorities = {
        priority
        for priority in (
            item.get("priority")
            for item in _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
            if isinstance(item, dict)
        )
        if isinstance(priority, int)
    }
    primary_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("customer_primary_demands")).get("items"))
        if isinstance(item, dict)
    ]
    indication_items = [
        item
        for item in _as_list(_as_dict(result_dict.get("standardized_indications")).get("items"))
        if isinstance(item, dict)
    ]
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    changed = False
    for item in items:
        matched_primary = _matching_primary_demand_for_recommendation(item, primary_items)
        if segments:
            keywords = _recommendation_keywords_for_item(item)
            original_evidence_text = _clean_text(item.get("evidence"))
            evidence = _find_supported_recommendation_evidence(
                segments,
                existing_evidence=original_evidence_text,
                keywords=keywords,
            )
            if evidence is None:
                changed = True
                continue
            if evidence != _clean_text(item.get("evidence")):
                item = {**item, "evidence": evidence}
                changed = True
            original_recommendation = _clean_text(item.get("recommendation"))
            original_product = _clean_text(item.get("product_or_solution"))
            natural_recommendation = _naturalize_staff_recommendation(
                original_recommendation or original_product,
                body_part=_clean_text(item.get("body_part")),
                evidence=evidence,
            )
            if (
                natural_recommendation
                and natural_recommendation != original_recommendation
                and _should_naturalize_recommendation_text(original_recommendation, original_product)
            ):
                item = {**item, "recommendation": natural_recommendation}
                if (
                    not original_product
                    or original_product == original_recommendation
                    or original_product in _known_plan_names()
                    or (
                        _recommendation_text_is_negated_alternative(original_product)
                        and _recommendation_evidence_has_positive_alternative(evidence)
                    )
                ):
                    item["product_or_solution"] = natural_recommendation
                changed = True
                refreshed_evidence = _find_supported_recommendation_evidence(
                    segments,
                    existing_evidence=original_evidence_text or evidence,
                    keywords=_recommendation_keywords_for_item(item),
                )
                if refreshed_evidence and refreshed_evidence != evidence:
                    item = {**item, "evidence": refreshed_evidence}
                    evidence = refreshed_evidence
                    changed = True
            if not _recommendation_evidence_supports_item(
                item,
                evidence,
                primary_items=primary_items,
                indication_items=indication_items,
            ):
                changed = True
                continue
            enriched_item, enriched_changed = _enrich_recommendation_item_from_evidence(item)
            if enriched_changed:
                item = enriched_item
                changed = True

        family = _recommendation_family(
            " ".join(
                part
                for part in (
                    _clean_text(item.get("recommendation")),
                    _clean_text(item.get("product_or_solution")),
                )
                if part
            )
        )
        body_part = _clean_text(item.get("body_part"))
        key = (family, body_part)
        if family and key in seen:
            changed = True
            continue
        if family:
            seen.add(key)
        if valid_demand_priorities:
            inferred_priorities = _infer_demand_priority(
                primary_items,
                body_part=body_part,
                text=_recommendation_context_text(item),
            )
            priorities: list[int] = []
            for raw_priority in _as_list(item.get("demand_priority")):
                try:
                    priority = int(raw_priority)
                except (TypeError, ValueError):
                    continue
                if priority in valid_demand_priorities and priority not in priorities:
                    priorities.append(priority)
            if inferred_priorities:
                priorities = inferred_priorities
            if not priorities and matched_primary and isinstance(matched_primary.get("priority"), int):
                priorities.append(int(matched_primary["priority"]))
            if priorities != _as_list(item.get("demand_priority")):
                item = {**item, "demand_priority": priorities}
                changed = True
        item_evidence = _clean_text(item.get("evidence"))
        duplicate_index = next(
            (
                index
                for index, kept_item in enumerate(kept)
                if item_evidence and _recommendation_items_look_duplicate(item, kept_item)
            ),
            None,
        )
        if duplicate_index is not None:
            existing_item = kept[duplicate_index]
            existing_text = _format_recommendation_plan_text(existing_item)
            new_text = _format_recommendation_plan_text(item)
            if len(new_text) > len(existing_text):
                kept[duplicate_index] = item
            changed = True
            continue
        kept.append(item)

    if not changed:
        return False

    recommendations["items"] = kept
    recommendations["summary"] = "；".join(
        _format_recommendation_plan_text(item)
        for item in kept[:3]
        if _format_recommendation_plan_text(item)
    )
    result_dict["staff_recommendations"] = recommendations
    return True


def _sync_consultation_result_recommended_plan(result_dict: dict[str, Any]) -> bool:
    recommendations = _as_dict(result_dict.get("staff_recommendations"))
    recommendation_items = [
        item for item in _as_list(recommendations.get("items")) if isinstance(item, dict)
    ]
    consultation_result = _as_dict(result_dict.get("consultation_result"))
    if not recommendation_items or not consultation_result:
        return False

    plan_items: list[dict[str, str]] = []
    for item in recommendation_items[:3]:
        plan = _format_recommendation_plan_text(item)
        if not plan:
            continue
        plan_items.append(
            {
                "plan": plan,
                "acceptance": _clean_text(item.get("customer_response")) or "未明确回应",
                "evidence": _clean_text(item.get("evidence")),
            }
        )

    if not plan_items:
        return False

    recommended_plan = _as_dict(consultation_result.get("recommended_plan"))
    new_summary = "；".join(item["plan"] for item in plan_items)
    changed = False
    if _clean_text(recommended_plan.get("summary")) != new_summary:
        recommended_plan["summary"] = new_summary
        changed = True
    if recommended_plan.get("items") != plan_items:
        recommended_plan["items"] = plan_items
        changed = True

    if not changed:
        return False

    consultation_result["recommended_plan"] = recommended_plan
    result_dict["consultation_result"] = consultation_result
    return True


def _sync_consultation_result_customer_profile_summary(result_dict: dict[str, Any]) -> bool:
    customer_profile = _as_dict(result_dict.get("customer_profile"))
    consultation_result = _as_dict(result_dict.setdefault("consultation_result", {}))
    tags = [item for item in _as_list(customer_profile.get("tags")) if isinstance(item, dict)]
    age = _clean_text(customer_profile.get("age")) or None
    age_evidence = _clean_text(customer_profile.get("age_evidence")) or None
    summary = f"本次录音共提取 {len(tags)} 个画像标签。" if tags else "本次录音暂未提取出明确画像标签。"
    next_summary = {
        "summary": summary,
        "extracted_tag_count": len(tags),
        "age": age,
        "age_evidence": age_evidence,
        "tags": tags,
    }
    if consultation_result.get("customer_profile_summary") == next_summary:
        return False
    consultation_result["customer_profile_summary"] = next_summary
    result_dict["consultation_result"] = consultation_result
    return True


def _looks_like_deal_success_question_or_hypothetical(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    success_terms = ("付款", "付钱", "交钱", "刷卡", "定金", "意向金", "下单", "开单", "验券", "核销", "今天做", "安排治疗")
    if any(hint in compact for hint in _DEAL_HYPOTHETICAL_HINTS) and any(term in compact for term in success_terms):
        return True
    if re.search(r"(?:怎么|如何).{0,8}(?:付款|付钱|交钱|刷卡|定金|下单|开单)", compact):
        return True
    if re.search(r"(?:付款|付钱|交钱|刷卡|定金|意向金|下单|开单|验券|核销|今天做|安排治疗).{0,8}(?:吗|呢|吧|行吗|可以吗|能吗|咋弄)", compact):
        return True
    return compact.endswith(("吗", "呢", "吧", "？", "?"))


def _looks_like_deal_flow_explanation_only(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if any(pattern.search(compact) for pattern in _DEAL_SUCCESS_COMPLETED_PATTERNS + _DEAL_SUCCESS_COMMITMENT_PATTERNS):
        return False
    if any(hint in compact for hint in _DEAL_SUCCESS_FLOW_ONLY_HINTS):
        return True
    if any(term in compact for term in ("刷卡", "定金", "付款", "今天做", "安排治疗")) and any(
        cue in compact for cue in ("可以", "能", "给你", "您", "你", "先", "如果")
    ):
        return True
    return False


def _has_strong_deal_success_signal(text: str) -> bool:
    compact = re.sub(r"\s+", "", _clean_text(text))
    if not compact:
        return False
    if _looks_like_deal_success_question_or_hypothetical(compact):
        return False
    if _looks_like_deal_flow_explanation_only(compact):
        return False
    if any(pattern.search(compact) for pattern in _DEAL_SUCCESS_COMPLETED_PATTERNS):
        return True
    return any(pattern.search(compact) for pattern in _DEAL_SUCCESS_COMMITMENT_PATTERNS)


def _backfill_consultation_result_outcome(result_dict: dict[str, Any], *, raw: dict[str, Any]) -> bool:
    consultation_result = _as_dict(result_dict.setdefault("consultation_result", {}))
    deal_outcome = _as_dict(consultation_result.setdefault("deal_outcome", {}))
    deal_factors = _as_dict(consultation_result.setdefault("deal_factors", {}))
    changed = False

    segments = _consultation_segments(raw)
    if not segments:
        return False

    outcome_keywords = (
        _DEAL_SUCCESS_HINTS
        + _DEAL_PENDING_HINTS
        + _DEAL_PRICE_LOSS_HINTS
        + _DEAL_SCHEDULE_LOSS_HINTS
        + _DEAL_EFFECT_LOSS_HINTS
        + tuple(keyword for _, keywords in _DECISION_FACTOR_HINTS for keyword in keywords)
    )
    supported_texts: list[str] = []
    for index, segment in enumerate(segments):
        text = _clean_text(segment.get("text"))
        if not text:
            continue
        if _is_customer_side_segment(segment) or _is_mislabeled_customer_candidate(segment, segments):
            supported_texts.append(text)
            continue
        if _confirmed_staff_segment_evidence(segments, index, keywords=outcome_keywords, allow_money=True):
            supported_texts.append(text)
    text_blob = "\n".join(supported_texts)
    existing_status = _clean_text(deal_outcome.get("status")) or "未明确"
    if existing_status not in {"已成交", "未成交", "未明确"}:
        existing_status = "未明确"
    has_strong_success = any(_has_strong_deal_success_signal(text) for text in supported_texts)
    has_pending_or_loss = any(
        keyword in text_blob for keyword in (_DEAL_PENDING_HINTS + _DEAL_PRICE_LOSS_HINTS + _DEAL_SCHEDULE_LOSS_HINTS)
    )
    inferred_status = existing_status
    if has_strong_success:
        inferred_status = "已成交"
    elif has_pending_or_loss:
        inferred_status = "未成交"
    elif existing_status == "已成交":
        inferred_status = "未明确"
    if inferred_status != existing_status:
        deal_outcome["status"] = inferred_status
        changed = True

    loss_reasons = _normalize_text_list(deal_outcome.get("loss_reasons"))
    current_status = _clean_text(deal_outcome.get("status")) or inferred_status
    if current_status == "未成交":
        if any(keyword in text_blob for keyword in _DEAL_PRICE_LOSS_HINTS):
            if "价格因素" not in loss_reasons:
                loss_reasons.append("价格因素")
                changed = True
        if any(keyword in text_blob for keyword in _DEAL_PENDING_HINTS):
            if "仍需考虑或商量" not in loss_reasons:
                loss_reasons.append("仍需考虑或商量")
                changed = True
        if any(keyword in text_blob for keyword in _DEAL_SCHEDULE_LOSS_HINTS):
            if "时间安排受限" not in loss_reasons:
                loss_reasons.append("时间安排受限")
                changed = True
        if any(keyword in text_blob for keyword in _DEAL_EFFECT_LOSS_HINTS):
            if "对效果或恢复仍有顾虑" not in loss_reasons:
                loss_reasons.append("对效果或恢复仍有顾虑")
                changed = True
    elif loss_reasons:
        loss_reasons = []
        changed = True
    if changed:
        deal_outcome["loss_reasons"] = loss_reasons

    decision_factors = _normalize_text_list(deal_factors.get("decision_factors"))
    if not decision_factors:
        for factor, keywords in _DECISION_FACTOR_HINTS:
            if any(keyword in text_blob for keyword in keywords):
                changed = _append_decision_factor(decision_factors, factor) or changed
    if decision_factors:
        deal_factors["decision_factors"] = _filter_overlapping_decision_factors(
            decision_factors,
            concern_texts=_normalize_text_list(deal_factors.get("concerns")),
            evidence_texts=[text_blob],
            loss_reasons=loss_reasons,
        )
    summary_parts = []
    budget = _clean_text(deal_factors.get("budget"))
    concern_texts = _normalize_text_list(deal_factors.get("concerns"))
    decision_factors = _normalize_text_list(deal_factors.get("decision_factors"))
    if budget:
        summary_parts.append(f"预算：{budget}")
    if concern_texts:
        summary_parts.append("客户顾虑：" + "；".join(concern_texts))
    if decision_factors:
        summary_parts.append("其他影响：" + "；".join(decision_factors))
    if summary_parts:
        deal_factors["summary"] = "；".join(summary_parts)

    consultation_result["deal_outcome"] = deal_outcome
    consultation_result["deal_factors"] = deal_factors
    result_dict["consultation_result"] = consultation_result
    return changed


def _split_dialogue(dialogue: str, target_size: int = _CHUNK_TARGET) -> list[str]:
    """将对话按行分成多段，每段不超过 target_size 字符。

    在行边界切分，确保不会切断一句话。
    """
    lines = dialogue.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current and current_len + line_len > target_size:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


_CHUNK_SYSTEM_PROMPT = """\
这是长录音的第{part_num}/{total}段。只基于本段可见证据抽取同一 JSON 结构：
- 正常信息量片段以正确率为先；宁可漏掉弱线索，也不要输出不确定结论。
- 本段没有证据的字段留空，不要补全或推测上下文。
- 若本段只是流程、闲聊或内部协作，不要制造主诉/适应症。
- 只有本段有效内容少且确认为医美业务场景时，才可最多兜底 1 条主诉/适应症，并写 inference_note。

"""

_MERGE_SYSTEM_PROMPT = """\
你是医美咨询结构化结果合并器。输入为同一长录音的多段 JSON，请输出一份完整 JSON。

合并原则：
1. 先确定同一主客户/主咨询线；剔除前台、候诊、内部协作、其他客户信息。
2. 正确率优先，宁可漏掉弱线索，也不要保留不确定结论。任何主诉、适应症、标签、顾虑、预算、推荐、成交结论的 evidence 不能独立支撑字段值时，删除该项；保留 [MM:SS]。
3. 冲突优先级：客户直接表达 > 客户确认 > 员工/医生明确方案 > 员工推断 > 含混表述。
4. 主诉按“诉求含义 + 部位”去重，保留证据强且描述清楚的一条，重排 priority；不把历史治疗、风格边界、否定表达、第三方案例或方案机制改成主诉。
5. 适应症按 indication_code + body_part_code 去重，编码整组保持原样；必须与主诉、明确方案或客户确认处理部位对齐。
6. 推荐方案按“方案路径 + 部位”去重，保留最完整证据；医生“用X填充/注射Y改善Z”也可作为明确方案。推荐方案必须和本次主诉/适应症同一咨询线；长录音后半段顺带聊到的其他部位、其他客户案例、顾问自用经历或泛化科普，不得覆盖开头主诉和医生正式方案。
7. 既往治疗后出现当前残留、凹陷、不满意、想修复/调整/恢复时，合并为当前问题；单纯历史只进治疗历史/负面项目线索。
8. 鼻基底/面中/苹果肌/八字纹在填充/注射语境下归面部填充；只有客户明确咨询鼻综合/隆鼻/鼻型手术方案才归鼻综合。泪沟/卧蚕在嗨体、胶原、玻尿酸、福曼等注射复配语境归塑美（眼部D）；眼袋/眶隔手术语境才归眼袋。体质/风险、否定/比较/假设不归正向事实。
9. 弱证据兜底只允许在“有效内容少 + 确认为医美业务场景 + 主诉或适应症为空”的极端情况下补 1 条；不得新增或覆盖其他分段已有强/中证据的主诉、适应症、推荐方案或 SAP 素材。
10. 年龄只接受客户直接年龄/出生日期回答；“我要是/如果/要是我/像XX岁/XX岁的时候/案例里XX岁”等效果范围、比较、假设、案例数字不得进入年龄、summary 或 SAP 素材。
11. consumption_intent、customer_concerns、customer_profile.tags 合并去重；budget 取最具体且最接近本次方案的金额/范围/支付线索。
12. sap_summary_materials 若有机构模板段落，保留该段落名和顺序；否则按默认 7 段合并为自然业务复盘，不补造事实。

只输出与原始分析相同结构的 JSON，不要输出其他内容。
"""


def _analyze_single(dialogue: str, system_prompt: str | None = None) -> dict:
    """单次 LLM 调用分析。"""
    sys_prompt = system_prompt or SYSTEM_PROMPT
    user_prompt = USER_PROMPT_TEMPLATE.format(dialogue=dialogue)
    logger.info(
        "Sending to LLM: system=%d user=%d total=%d chars",
        len(sys_prompt),
        len(user_prompt),
        len(sys_prompt) + len(user_prompt),
    )
    return _call_llm_json(
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
    )


def _analyze_chunked(dialogue: str, system_prompt: str | None = None) -> dict:
    """将超长对话分段分析后合并。"""
    sys_prompt = system_prompt or SYSTEM_PROMPT
    chunks = _split_dialogue(dialogue)
    logger.info("Dialogue too long (%d chars), splitting into %d chunks",
                len(dialogue), len(chunks))

    partial_results: list[dict] = []
    for i, chunk in enumerate(chunks):
        part_num = i + 1
        system = sys_prompt + "\n\n" + _CHUNK_SYSTEM_PROMPT.format(
            part_num=part_num, total=len(chunks)
        )
        user_prompt = USER_PROMPT_TEMPLATE.format(dialogue=chunk)
        logger.info(
            "Analyzing chunk %d/%d: dialogue=%d system=%d user=%d total=%d chars",
            part_num,
            len(chunks),
            len(chunk),
            len(system),
            len(user_prompt),
            len(system) + len(user_prompt),
        )
        partial = _call_llm_json(
            system_prompt=system,
            user_prompt=user_prompt,
        )
        partial_results.append(partial)
        logger.info("Chunk %d/%d done", part_num, len(chunks))

    # 合并阶段
    merge_user_prompt = "以下是同一段对话分 {} 段分析的结果，请合并为一份完整报告：\n\n".format(
        len(partial_results)
    )
    for i, pr in enumerate(partial_results):
        merge_user_prompt += f"=== 第 {i+1} 段分析结果 ===\n"
        merge_user_prompt += json.dumps(pr, ensure_ascii=False, separators=(",", ":"))
        merge_user_prompt += "\n\n"

    logger.info(
        "Merging %d partial results: system=%d user=%d total=%d chars",
        len(partial_results),
        len(_MERGE_SYSTEM_PROMPT),
        len(merge_user_prompt),
        len(_MERGE_SYSTEM_PROMPT) + len(merge_user_prompt),
    )
    return _call_llm_json(
        system_prompt=_MERGE_SYSTEM_PROMPT,
        user_prompt=merge_user_prompt,
        max_tokens=12000,
    )


def _compute_inference_note(raw: dict) -> str | None:
    """根据原始转写数据中的角色分布，判断是否需要添加推断说明。

    当客户发言占比不足 20% 时，自动生成 inference_note。
    """
    segs = _consultation_segments(raw)
    if not segs:
        return None
    total = len(segs)
    customer_count = sum(1 for s in segs if _is_customer_side_segment(s))
    ratio = customer_count / total
    if ratio < 0.20:
        if customer_count == 0:
            if any(_is_mislabeled_customer_candidate(segment, segs) for segment in segs):
                return "本次录音中客户可能被标记为员工同事，以下内容依据顾客侧语义与咨询师回应推断"
            return "本次录音中客户发言未被单独识别，以下内容主要基于咨询师的面诊判断与方案讲解推断"
        return (
            f"本次对话中客户发言较少（仅占{ratio:.0%}），"
            "以下内容主要基于咨询师的表述及客户的认同反应推断"
        )
    return None


def analyze_transcript(path: str | Path, *, system_prompt: str | None = None) -> AnalysisResult:
    """对一个转写 JSON 文件执行完整的四项分析。

    对于超长对话（>{} 字符），自动分段分析后合并。
    传入 system_prompt 则使用动态提示词，否则使用静态默认值。
    """.format(_CHUNK_THRESHOLD)
    path = Path(path)
    logger.info("Loading transcript: %s", path.name)

    dialogue, raw = prepare_transcript(path)

    if not dialogue.strip():
        raise ValueError(f"转写文件 {path.name} 中没有有效对话内容")

    logger.info("Transcript formatted: %d chars", len(dialogue))

    if len(dialogue) > _CHUNK_THRESHOLD:
        result_dict = _analyze_chunked(dialogue, system_prompt=system_prompt)
    else:
        result_dict = _analyze_single(dialogue, system_prompt=system_prompt)

    # 基于角色分布自动注入 inference_note
    note = _compute_inference_note(raw)
    if note:
        for key in (
            "customer_primary_demands",
            "standardized_indications",
            "customer_demands",
            "customer_concerns",
            "customer_profile",
        ):
            if key in result_dict:
                result_dict[key]["inference_note"] = note

    _sanitize_customer_primary_demands(result_dict, raw=raw)
    _sanitize_standardized_indications(result_dict, raw=raw)
    first_item_fallback_changed = _backfill_first_consultation_item(result_dict, raw=raw)
    _backfill_customer_profile_tags(result_dict, raw=raw)
    _backfill_consumption_intent(result_dict, raw=raw)
    _backfill_customer_concerns(result_dict, raw=raw)
    _sanitize_customer_profile_tags(result_dict, raw=raw)
    _sanitize_consumption_intent(result_dict, raw=raw)
    _sanitize_customer_concerns(result_dict, raw=raw)
    _backfill_customer_concerns(result_dict, raw=raw)
    _sanitize_customer_concerns(result_dict, raw=raw)
    _backfill_staff_recommendations(result_dict, raw=raw)
    _sanitize_staff_recommendations(result_dict, raw=raw)
    _backfill_staff_recommendations(result_dict, raw=raw)
    _sanitize_staff_recommendations(result_dict, raw=raw)
    _backfill_consultation_result_outcome(result_dict, raw=raw)
    _sync_consultation_result_recommended_plan(result_dict)

    standardized_indications = result_dict.get("standardized_indications")
    if isinstance(standardized_indications, dict):
        result_dict["standardized_indications"] = normalize_standardized_indications_payload(standardized_indications)

    # Safety net: medical consultation with recommendations must have indications
    _si = _as_dict(result_dict.get("standardized_indications"))
    _recs = _as_list(_as_dict(result_dict.get("staff_recommendations")).get("items"))
    if not _as_list(_si.get("items")) and _recs:
        segments = _consultation_segments(raw)
        _backfill_standardized_indications(
            _si,
            segments=segments,
            primary_demand_payload=_as_dict(result_dict.get("customer_primary_demands")),
            staff_recommendations_payload=_as_dict(result_dict.get("staff_recommendations")),
        )
        result_dict["standardized_indications"] = normalize_standardized_indications_payload(_si)

    _backfill_primary_demands_from_plan_context(result_dict, raw=raw)
    _backfill_empty_standardized_indications(result_dict, raw=raw)
    _sanitize_standardized_indications(result_dict, raw=raw)

    result_dict["consultation_evaluation"] = rebuild_consultation_evaluation(result_dict, dialogue=dialogue)
    result_dict["consultation_process_evaluation"] = rebuild_consultation_process_evaluation(
        result_dict,
        dialogue=dialogue,
    )

    from smart_badge_api.api.analysis_normalization import normalize_analysis_result

    if first_item_fallback_changed:
        _clear_stale_first_item_summary(result_dict)

    normalized_result = normalize_analysis_result(result_dict)
    if isinstance(normalized_result, dict):
        result_dict = normalized_result
    _sanitize_customer_profile_tags(result_dict, raw=raw)
    _backfill_primary_demands_from_plan_context(result_dict, raw=raw)
    _backfill_empty_standardized_indications(result_dict, raw=raw)
    _sanitize_standardized_indications(result_dict, raw=raw)
    _sanitize_staff_recommendations(result_dict, raw=raw)
    _backfill_staff_recommendations(result_dict, raw=raw)
    _sanitize_staff_recommendations(result_dict, raw=raw)
    _sync_consultation_result_recommended_plan(result_dict)
    _sync_consultation_result_customer_profile_summary(result_dict)

    result = AnalysisResult.model_validate(result_dict)

    logger.info("Analysis complete for %s", path.name)
    return result
