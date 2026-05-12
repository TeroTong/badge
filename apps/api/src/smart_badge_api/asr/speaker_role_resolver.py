from __future__ import annotations

import re
from dataclasses import dataclass

SEMANTIC_ROLES = frozenset({"consultant", "doctor", "customer", "unknown"})
STAFF_ROLES = frozenset({"consultant", "doctor"})
VISITOR_ROLES = frozenset({"customer"})

BUSINESS_ROLE_TO_COARSE_ROLE = {
    "badge_owner": "consultant",
    "staff_peer": "consultant",
    "doctor": "doctor",
    "primary_customer": "customer",
    "visitor_companion": "customer",
}

BUSINESS_ROLE_LABELS = {
    "badge_owner": "工牌本人",
    "staff_peer": "员工同事",
    "doctor": "医生",
    "primary_customer": "主客户",
    "visitor_companion": "同行人",
    "unknown": "未识别参与者",
}

_RAW_SPEAKER_PATTERN = re.compile(r"^speaker_\d+$", re.IGNORECASE)
_STAFF_INTRO_PATTERNS = (
    "您好",
    "欢迎",
    "请问",
    "想咨询什么",
    "今天想咨询",
    "我先了解",
    "我先帮您",
    "我帮您",
    "帮您看",
    "我们医院",
    "我们这边",
    "咱们",
    "建议",
    "适合",
    "方案",
    "接待您",
)
_CUSTOMER_PATTERNS = (
    "我想",
    "我主要",
    "我觉得",
    "我担心",
    "我怕",
    "我之前",
    "我没有",
    "我做过",
    "想改善",
    "想了解",
    "多少钱",
    "会不会",
    "疼不疼",
    "我的",
)
_NEGATIVE_HISTORY_ANSWER_HINTS = (
    "从来没打过",
    "从来没有打过",
    "从来没做过",
    "从来没有做过",
    "没有做过医美",
    "没做过医美",
    "没有做过项目",
    "没做过项目",
    "没有做过皮肤项目",
    "没做过皮肤项目",
    "也没做过皮肤项目",
    "没有打过",
    "没打过",
    "没有暴晒",
    "没暴晒",
    "美容院也没去过",
    "没去过美容院",
)
_THIRD_PARTY_HISTORY_HINTS = (
    "老乡",
    "朋友",
    "姐妹",
    "同事",
    "别人",
    "其他顾客",
    "其他客户",
    "案例",
)
_COMPANION_PATTERNS = (
    "我陪她来",
    "我陪他来",
    "陪她来的",
    "陪他来的",
    "陪她过来",
    "陪他过来",
    "陪同",
    "跟她一起来",
    "跟他一起来",
    "带她来",
    "带他来",
    "替她问",
    "替他问",
    "帮她问",
    "帮他问",
    "她想了解",
    "他想了解",
    "她想做",
    "他想做",
    "她要做",
    "他要做",
    "她担心",
    "他担心",
)
_DOCTOR_PATTERNS = (
    "从医学角度",
    "解剖",
    "层次",
    "组织",
    "皮下",
    "筋膜",
    "禁忌",
    "风险",
    "麻醉",
    "术后",
    "恢复期",
    "存活率",
    "疤痕松解",
    "松解",
    "塑形",
    "基础条件",
    "腰侧",
    "后背",
    "手臂",
    "填一点",
    "填充",
    "微针",
    "眼周",
    "形态",
)
_QUESTION_PATTERNS = ("？", "?", "吗", "呢", "请问", "有没有", "会不会", "能不能", "是否")
_GENERIC_STAFF_INTRO_PATTERNS = (
    "我是今天接待您的",
    "我是今天给您面诊的",
    "我是您的咨询师",
    "我是您的医生",
    "今天接待您的",
)
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？；,.!?]+")
_LEADING_FILLER_RE = re.compile(r"^(?:嗯|啊|唉|哎|诶|哦|噢|哈|呀|呃|额|那|这个|就是|然后|所以|嘛)+")
_SHORT_CUSTOMER_CONFIRMATIONS = frozenset(
    {
        "对",
        "嗯",
        "嗯嗯",
        "是",
        "是的",
        "不是",
        "没有",
        "还没有",
        "有",
        "有的",
        "可以",
        "可以的",
        "不知道",
        "不太确定",
        "了解过",
        "没了解过",
        "做过",
        "没做过",
        "第一次",
        "还好",
        "不考虑",
    }
)
_CUSTOMER_RESPONSE_HINTS = (
    "我想",
    "我主要",
    "我担心",
    "我怕",
    "我自己",
    "我的预算",
    "我的主要诉求",
    "我目前",
    "我回去",
    "我要",
    "我打算",
    "我的意思",
    "我父母",
    "我妈妈",
    "我还",
    "我没有",
    "我做过",
    "从来没",
    "从来没有",
    "没有做过",
    "没做过",
    "没有打过",
    "没打过",
    "没去过",
    "没有暴晒",
    "没暴晒",
    "第一次",
    "没了解过",
    "不了解",
    "还没有",
    "马上工作",
    "刚毕业",
    "工作已经找好了",
    "预算",
    "预算是",
    "自然",
    "恢复期",
    "回去考虑",
    "考虑一下",
    "商量",
    "帮我算",
    "给我算",
    "算一下价格",
    "给我看一下",
    "找父母",
    "跟父母",
    "分期",
    "短期分期",
    "看不出来",
    "国企",
    "体制内",
    "5月份",
    "6月份",
    "一个多月",
)
_CUSTOMER_SELF_REPORT_HINTS = (
    "我的预算",
    "我的主要诉求",
    "我手上有",
    "我脸上有",
    "我鼻子",
    "我下巴",
    "我太阳穴",
    "我额头",
    "我嘴巴",
    "我皮肤",
    "我这边",
    "我这里",
    "我还有",
    "我就",
    "我脸上",
    "我手上",
)
_CUSTOMER_SELF_REPORT_ISSUE_HINTS = (
    "疤",
    "疤痕",
    "痘印",
    "痘坑",
    "长痘",
    "痘痘",
    "肤质",
    "凹陷",
    "下垂",
    "内陷",
    "反光",
    "留的",
    "有一块",
    "有点",
    "宽一些",
    "高低不一样",
)
_STAFF_ADVICE_HINTS = (
    "建议",
    "可以做",
    "可以打",
    "做光子",
    "刷酸",
    "水杨酸",
    "光子嫩肤",
    "玻尿酸",
    "填充",
    "手术",
    "方案",
    "改善一下",
    "你现在",
    "你这个",
)
_STAFF_SELF_EXAMPLE_HINTS = (
    "我个人觉得",
    "我觉得还好",
    "我手上也有",
    "我也有",
    "我自己打过",
    "我自己也打",
    "像我的话",
    "像我",
    "我当时",
    "我那时候",
    "我们员工",
    "我同事",
    "有个顾客",
    "有个客户",
    "其他顾客",
    "其他客户",
    "案例",
    "给我发微信",
    "你看这",
)
_STAFF_STATEMENT_HINTS = (
    *_STAFF_INTRO_PATTERNS,
    *_GENERIC_STAFF_INTRO_PATTERNS,
    "我先",
    "我给你",
    "我帮你",
    "我带你",
    "我带您",
    "我带下一位",
    "带下一位",
    "带客户",
    "带你过去",
    "带您过去",
    "带你去",
    "带您去",
    "东西带好",
    "坐对面",
    "对面房间",
    "接待你",
    "美学设计师",
    "美学顾问",
    "咨询师",
    "设计师",
    "你最好",
    "你可以",
    "你主要",
    "你想解决",
    "你是想",
    "我们这边",
    "医生这边",
    "陈倩院长",
    "陈谦院长",
    "刘健院长",
    "做不到",
    "其他医生",
    "其他顾客",
    "其他客户",
    "案例",
    "给你看",
    "院长",
    "方案",
    "材料",
    "假体",
    "手术",
    "恢复",
    "优惠",
    "给你报价",
    "给您报价",
    "帮你算价格",
    "帮您算价格",
    "建议",
    "其实",
    "为什么",
)
_CUSTOMER_QUESTION_HINTS = (
    "是什么",
    "什么",
    "哪些",
    "多大",
    "做过",
    "了解过",
    "多久",
    "什么时候",
    "打算",
    "考虑",
    "有没有",
    "会不会",
    "能不能",
    "是不是",
    "你还",
    "你要",
    "你是",
    "你自己",
    "你妈妈",
    "你父母",
)
_EXPLICIT_BADGE_OWNER_SOURCES = frozenset(
    {
        "explicit_staff_intro",
        "explicit_staff_intro_context",
    }
)
_EXPLICIT_PRIMARY_CUSTOMER_SOURCES = frozenset(
    {
        "explicit_customer_treatment_history",
        "explicit_customer_treatment_history_context",
    }
)
_CONTENT_OVERRIDE_ROLE_SOURCES = frozenset(
    {
        "content_non_doctor_staff",
        "content_doctor_explanation",
        "content_staff_explanation_context",
    }
)
_PER_UTTERANCE_ROLE_SOURCES = (
    _EXPLICIT_BADGE_OWNER_SOURCES
    | _EXPLICIT_PRIMARY_CUSTOMER_SOURCES
    | _CONTENT_OVERRIDE_ROLE_SOURCES
)
_STALE_HEURISTIC_ROLE_SOURCES = frozenset({"local_heuristic"})
_STAFF_SELF_INTRO_ROLE_HINTS = (
    "负责现场接待",
    "现场接待",
    "接待你",
    "接待您",
    "服务你",
    "服务您",
    "美学设计师",
    "美学顾问",
    "咨询师",
    "咨询顾问",
    "顾问",
    "医生",
    "院长",
    "护士",
    "老师",
    "专家助理",
    "医生助理",
    "医助",
    "院长助理",
    "咨询助理",
)
_NON_DOCTOR_STAFF_IDENTITY_HINTS = (
    "专家助理",
    "医生助理",
    "医助",
    "院长助理",
    "咨询助理",
    "美学顾问",
    "美学设计师",
    "现场接待",
)
_NON_DOCTOR_STAFF_DELEGATION_HINTS = (
    "王院长的手术",
    "约了我们王院长",
    "我去看一下他的手术",
    "我去看一下手术",
    "如果快结束",
    "先让他看一下",
    "帮我面诊",
    "喊他面诊",
    "我带顾客来",
    "我带您到手术室",
    "手术进展",
    "在手术不",
    "没上手术",
    "下一台手术",
    "划线",
    "消毒",
    "换衣服",
    "重新洗手",
)
_STAFF_EXPLANATION_ADDRESS_HINTS = (
    "给你讲一下",
    "给您讲一下",
    "你看",
    "您看",
    "你这个",
    "您这个",
    "你的",
    "您的",
    "对你的影响",
    "对您的影响",
    "你要清楚",
    "您要清楚",
    "我要告诉你",
    "我要告诉您",
    "我可以把你",
    "我可以把您",
    "我给你",
    "我给您",
    "是不是感觉",
)
_DOCTOR_EXPLANATION_MEDICAL_HINTS = (
    "眼袋",
    "泪沟",
    "苹果肌",
    "上眼窝",
    "脂肪",
    "内切",
    "外切",
    "回填",
    "填充",
    "凹陷",
    "存活率",
    "术前",
    "模拟",
    "皮肤松弛",
    "脂肪渗",
    "屏障功能",
    "手术",
    "麻药",
    "局麻",
    "拆线",
    "恢复期",
    "遗传",
)
_DOCTOR_EXPLANATION_FLOW_HINTS = (
    "不仅仅是",
    "除了",
    "其实",
    "为什么",
    "正常你",
    "正常您",
    "整体",
    "我术前",
    "通过模拟",
    "推平整",
    "分开来",
    "合在一起看",
    "我要告诉",
)
_STAFF_EXPLANATION_FOLLOWUP_HINTS = (
    "这边",
    "这一块",
    "这个地方",
    "包括这边",
    "是不是感觉",
    "好一些",
    "明显",
    "凹进去",
    "推平整",
    "脂肪",
    "眼袋",
    "泪沟",
)
_NON_DOCTOR_STAFF_CONTEXT_HINTS = (
    "王院长",
    "手术",
    "顾客",
    "面诊",
    "门口",
    "案例",
    "稍等",
    "先带",
    "带您",
    "带你",
    "这边",
    "那边",
    "划线",
    "消毒",
    "换衣服",
    "电话",
)
_STAFF_SERVICE_FOLLOWUP_HINTS = (
    "他们刚刚跟我说",
    "刚刚跟我说",
    "我去给你",
    "我去给您",
    "我给你找",
    "我给您找",
    "给你找个",
    "给您找个",
    "我帮你",
    "我帮您",
    "我带你",
    "我带您",
    "我先带",
)
_CUSTOMER_TREATMENT_HISTORY_PARTS = (
    "眉弓",
    "鼻子",
    "嘴巴",
    "下巴",
    "耳朵",
    "全脸",
    "双眼皮",
    "眼睛",
    "额头",
    "太阳穴",
    "苹果肌",
    "法令纹",
    "鼻基底",
    "脸",
)
_CUSTOMER_TREATMENT_HISTORY_VERBS = (
    "打过",
    "做过",
    "动过",
    "填过",
    "垫过",
    "割过",
    "埋过",
    "注射过",
)
_CUSTOMER_TREATMENT_HISTORY_FOLLOWUP_HINTS = (
    "双眼皮也做过",
    "没有动手术",
    "打了瘦脸针",
    "瘦脸针之后",
    "我脸上花了好多钱",
    "我全脸都动过",
    "我全脸都打过",
)
_ROLE_RESET_FIELDS = (
    "speaker_role",
    "speaker_role_source",
    "speaker_business_role",
    "speaker_identity_type",
    "speaker_display_label",
    "speaker_staff_id",
    "speaker_staff_name",
    "speaker_voiceprint_similarity",
)


@dataclass(slots=True)
class _SpeakerStats:
    key: str
    first_begin_ms: int
    utterance_count: int = 0
    total_duration_ms: int = 0
    total_chars: int = 0
    question_hits: int = 0
    staff_keyword_hits: int = 0
    customer_keyword_hits: int = 0
    companion_keyword_hits: int = 0
    doctor_keyword_hits: int = 0
    generic_staff_intro_hits: int = 0
    staff_name_hits: int = 0
    polite_you_hits: int = 0
    self_reference_hits: int = 0

    @property
    def staff_score(self) -> float:
        duration_bonus = min(self.total_duration_ms / 15_000.0, 3.0)
        chars_bonus = min(self.total_chars / 80.0, 3.0)
        return (
            self.staff_name_hits * 5.0
            + self.generic_staff_intro_hits * 3.0
            + self.staff_keyword_hits * 1.4
            + self.question_hits * 0.8
            + self.polite_you_hits * 0.25
            + duration_bonus
            + chars_bonus
        )

    @property
    def customer_score(self) -> float:
        return (
            self.customer_keyword_hits * 1.5
            + self.question_hits * 0.5
            + min(self.self_reference_hits / 6.0, 2.0)
            + (1.0 if self.utterance_count <= 4 else 0.0)
        )

    @property
    def companion_score(self) -> float:
        return (
            self.companion_keyword_hits * 2.2
            + (0.8 if self.utterance_count <= 4 else 0.0)
        )

    @property
    def primary_customer_score(self) -> float:
        duration_bonus = min(self.total_duration_ms / 45_000.0, 2.5)
        return (
            self.customer_score * 1.5
            + duration_bonus
            - self.companion_score * 1.6
            - max(self.doctor_score - self.customer_score, 0.0) * 0.4
        )

    @property
    def doctor_score(self) -> float:
        duration_bonus = min(self.total_duration_ms / 20_000.0, 2.0)
        return (
            self.doctor_keyword_hits * 1.8
            + self.generic_staff_intro_hits * 1.5
            + self.staff_name_hits * 0.5
            + duration_bonus
        )


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_role(value: object) -> str:
    text = _clean_text(value).lower()
    if text in SEMANTIC_ROLES:
        return text
    return "unknown"


def _normalize_staff_role(value: object) -> str:
    return "doctor" if _clean_text(value).lower() == "doctor" else "consultant"


def _normalize_business_role(value: object) -> str:
    text = _clean_text(value).lower()
    if text in BUSINESS_ROLE_LABELS:
        return text
    return "unknown"


def _is_raw_speaker_label(value: object) -> bool:
    return bool(_RAW_SPEAKER_PATTERN.match(_clean_text(value)))


def _speaker_key(utterance: dict) -> str:
    speaker_id = _clean_text(utterance.get("speaker_id"))
    if speaker_id:
        return speaker_id
    speaker = _clean_text(utterance.get("speaker"))
    return speaker or "unknown"


def _count_hits(text: str, patterns: tuple[str, ...]) -> int:
    return sum(1 for pattern in patterns if pattern in text)


def _speaker_group_values(utterances: list[dict], field: str) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        if _clean_text(utterance.get("speaker_role_source")) in _PER_UTTERANCE_ROLE_SOURCES:
            continue
        value = _clean_text(utterance.get(field))
        if not value:
            continue
        grouped.setdefault(_speaker_key(utterance), set()).add(value)
    return grouped


def _speaker_semantic_roles(utterances: list[dict]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        speaker_key = _speaker_key(utterance)
        grouped.setdefault(speaker_key, set())
        source = _clean_text(utterance.get("speaker_role_source"))
        current_speaker = utterance.get("speaker")
        allow_semantic_hint = source in {
            "upstream",
            "local_heuristic",
            "mixed_turn_split",
            "voiceprint",
            "voiceprint_bound_staff",
            "voiceprint_counterparty",
        } or (not source and not _is_raw_speaker_label(current_speaker))
        if not allow_semantic_hint:
            continue
        for field in ("speaker_role", "speaker"):
            role = _normalize_role(utterance.get(field))
            if role != "unknown":
                grouped[speaker_key].add(role)
    return grouped


def _normalize_fragment_text(text: str) -> str:
    cleaned = _clean_text(text).strip("，。！？；,.!?:：;~～ ")
    if not cleaned:
        return ""
    without_fillers = _LEADING_FILLER_RE.sub("", cleaned, count=1).strip("，。！？；,.!?:：;~～ ")
    return without_fillers or cleaned


def _compact_fragment_text(text: str) -> str:
    return re.sub(r"\s+", "", _normalize_fragment_text(text))


def _looks_like_customer_negative_history_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    compact = _compact_fragment_text(text)
    if not normalized or len(compact) > 80:
        return False
    if compact.startswith(("你", "您", "有没有", "有没", "是否")):
        return False
    negative_answer_prefix = compact.startswith("没有") and not compact.startswith(("有没有", "有没"))
    if ("有没有" in compact or "有没" in compact) and not negative_answer_prefix:
        return False
    if any(hint in compact for hint in _THIRD_PARTY_HISTORY_HINTS):
        return False
    if any(hint in compact for hint in ("如果", "假如", "像你这种", "一般客户")):
        return False
    if any(hint in compact for hint in _NEGATIVE_HISTORY_ANSWER_HINTS):
        return True
    return bool(
        re.search(r"(?:^|之前|以前|一直|也)(?:都)?(?:从来)?(?:没|没有|未)(?:有)?(?:做过|打过|去过)", compact)
        or re.search(r"(?:^|最近|这段时间)(?:都)?(?:没|没有)(?:有)?(?:暴晒|晒过)", compact)
    )


def _looks_like_customer_question_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    if not normalized:
        return False
    if _looks_like_customer_negative_history_fragment(normalized):
        return False
    if "我" in normalized and any(cue in normalized for cue in ("回去考虑", "考虑一下", "商量", "找父母", "跟父母")):
        return False
    if any(cue in normalized for cue in _CUSTOMER_QUESTION_HINTS):
        return True
    if "你" in normalized and normalized.endswith(("吗", "呢", "呀", "吧", "？", "?")):
        return True
    return False


def _looks_like_staff_bridge_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    return bool(re.fullmatch(r"[0-9一二两三四五六七八九十百千万.,，]+的话", normalized))


def _looks_like_customer_answer_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    if not normalized:
        return False
    if _looks_like_customer_negative_history_fragment(normalized):
        return True
    if (
        "我" in normalized
        and len(normalized) <= 90
        and any(
            hint in normalized
            for hint in (
                "预算",
                "主要诉求",
                "我的意思",
                "回去考虑",
                "考虑一下",
                "商量",
                "找父母",
                "跟父母",
                "分期",
                "看不出来",
            )
        )
        and not any(hint in normalized for hint in ("我先", "我帮您", "我给您", "我带您", "我建议"))
    ):
        return True
    if len(normalized) > 36:
        return False
    if _looks_like_customer_question_fragment(normalized):
        return False
    if _looks_like_staff_statement_fragment(normalized):
        return False
    if normalized in _SHORT_CUSTOMER_CONFIRMATIONS:
        return True
    if re.fullmatch(r"(?:\d+|[一二两三四五六七八九十百]+)(?:岁|个月|月|天)?", normalized):
        return True
    if any(hint in normalized for hint in _CUSTOMER_RESPONSE_HINTS):
        if "我" not in normalized and sum(1 for hint in _STAFF_STATEMENT_HINTS if hint in normalized) >= 2:
            return False
        return True
    if "我" in normalized and len(normalized) <= 24:
        return True
    return False


def _looks_like_customer_self_report_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    if not normalized or len(normalized) > 90:
        return False
    if _looks_like_customer_negative_history_fragment(normalized):
        return True
    if _looks_like_customer_question_fragment(normalized):
        return False
    if any(hint in normalized for hint in _STAFF_SELF_EXAMPLE_HINTS):
        return False
    if sum(1 for hint in _STAFF_STATEMENT_HINTS if hint in normalized) >= 2:
        return False
    if "我" not in normalized:
        return False
    if "你" in normalized and any(hint in normalized for hint in _STAFF_ADVICE_HINTS):
        return False
    if any(hint in normalized for hint in _CUSTOMER_SELF_REPORT_HINTS):
        return True
    if any(hint in normalized for hint in _CUSTOMER_SELF_REPORT_ISSUE_HINTS) and re.search(
        r"我(?:的|脸上|手上|鼻子|下巴|太阳穴|额头|嘴巴|皮肤|这边|这里)?",
        normalized,
    ):
        return True
    return False


def _looks_like_staff_statement_fragment(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    if not normalized:
        return False
    if any(hint in normalized for hint in _STAFF_STATEMENT_HINTS):
        return True
    if any(hint in normalized for hint in _STAFF_ADVICE_HINTS):
        return True
    if "您" in normalized or "我们" in normalized:
        return True
    return False


def _find_embedded_question_answer_split(text: str) -> tuple[str, str] | None:
    normalized = _clean_text(text)
    if not normalized or len(normalized) < 8:
        return None

    candidate_offsets: list[int] = []
    for marker in ("？", "?", "对不对", "是不是", "有没有", "会不会", "能不能"):
        search_from = 0
        while True:
            index = normalized.find(marker, search_from)
            if index < 0:
                break
            candidate_offsets.append(index + len(marker))
            search_from = index + len(marker)
    for index, char in enumerate(normalized):
        if char in {"吗", "呢", "吧"} and index + 1 < len(normalized):
            candidate_offsets.append(index + 1)

    for offset in sorted(set(candidate_offsets)):
        head = normalized[:offset].strip("，。！？；,.!?:：;~～ ")
        tail = normalized[offset:].strip("，。！？；,.!?:：;~～ ")
        if len(head) < 4 or not tail:
            continue
        if _looks_like_customer_answer_fragment(tail):
            return head, tail
    return None


def _find_embedded_customer_staff_split(text: str) -> tuple[str, str] | None:
    normalized = _clean_text(text)
    if not normalized or len(normalized) < 8:
        return None

    budget_match = re.search(
        r"(.{0,24}?(?:我的)?预算(?:是|大概是|就)?[0-9一二两三四五六七八九十百千万.,，]+)\s+([0-9一二两三四五六七八九十百千万.,，]+的话.*)$",
        normalized,
    )
    if budget_match:
        head = budget_match.group(1).strip("，。！？；,.!?:：;~～ ")
        tail = budget_match.group(2).strip("，。！？；,.!?:：;~～ ")
        if head and tail:
            return head, tail

    for marker in ("你最好", "你可以", "你主要", "你想解决", "你是想", "陈倩院长", "陈谦院长", "刘健院长", "其他医生", "做不到"):
        index = normalized.find(marker)
        if index <= 0:
            continue
        head = normalized[:index].strip("，。！？；,.!?:：;~～ ")
        tail = normalized[index:].strip("，。！？；,.!?:：;~～ ")
        if not head or not tail:
            continue
        if (
            _looks_like_customer_answer_fragment(head)
            or _looks_like_customer_self_report_fragment(head)
        ) and _looks_like_staff_statement_fragment(tail):
            return head, tail
    return None


def _split_text_clauses(text: str) -> list[str]:
    fragments: list[str] = []
    # ASR sometimes writes amounts as "25,000"; do not split numeric commas
    # into separate dialogue fragments.
    normalized_text = re.sub(r"(?<=\d)[,，](?=\d)", "", _clean_text(text))
    for raw_clause in _CLAUSE_SPLIT_RE.split(normalized_text):
        clause = _clean_text(raw_clause)
        if not clause:
            continue
        pending = [clause]
        while pending:
            current = pending.pop(0)
            embedded = _find_embedded_question_answer_split(current)
            if embedded is None:
                embedded = _find_embedded_customer_staff_split(current)
            if embedded is None:
                fragments.append(current)
                continue
            head, tail = embedded
            fragments.append(head)
            pending.insert(0, tail)
    return fragments or [_clean_text(text)]


def _should_split_badge_owner_utterance(text: str) -> bool:
    clauses = _split_text_clauses(text)
    if len(clauses) <= 1:
        return False
    for index, clause in enumerate(clauses):
        is_customer_answer = _looks_like_customer_answer_fragment(clause)
        is_customer_self_report = _looks_like_customer_self_report_fragment(clause)
        if not (is_customer_answer or is_customer_self_report):
            continue
        prev_clause = clauses[index - 1] if index > 0 else ""
        next_clause = clauses[index + 1] if index + 1 < len(clauses) else ""
        next_next_clause = clauses[index + 2] if index + 2 < len(clauses) else ""
        if (
            _looks_like_customer_question_fragment(prev_clause)
            or _looks_like_customer_question_fragment(next_clause)
            or _looks_like_staff_statement_fragment(next_clause)
            or (
                _looks_like_staff_bridge_fragment(next_clause)
                and _looks_like_staff_statement_fragment(next_next_clause)
            )
            or (index > 0 and _looks_like_staff_statement_fragment(prev_clause))
        ):
            return True
    return False


def _timed_fragments(fragments: list[str], begin_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    duration_ms = max(end_ms - begin_ms, len(fragments) * 120)
    weights = [max(len(_normalize_fragment_text(fragment)), 1) for fragment in fragments]
    total_weight = max(sum(weights), 1)
    cursor_weight = 0
    timed: list[tuple[str, int, int]] = []
    for index, fragment in enumerate(fragments):
        fragment_begin = begin_ms + round(duration_ms * cursor_weight / total_weight)
        cursor_weight += weights[index]
        fragment_end = end_ms if index == len(fragments) - 1 else begin_ms + round(duration_ms * cursor_weight / total_weight)
        if fragment_end < fragment_begin:
            fragment_end = fragment_begin
        timed.append((fragment, fragment_begin, fragment_end))
    return timed


def _looks_like_standalone_customer_turn(text: str) -> bool:
    normalized = _normalize_fragment_text(text)
    if not normalized or len(normalized) > 100:
        return False
    if _looks_like_badge_owner_self_intro_fragment(normalized, None):
        return False
    if any(hint in normalized for hint in _STAFF_SELF_EXAMPLE_HINTS):
        return False
    if any(hint in normalized for hint in ("你可以叫我", "您可以叫我", "叫我", "比你大", "比您大", "你多大", "您多大")):
        return False
    if any(hint in normalized for hint in ("您", "我们这边", "我帮您", "我给您", "我带您")):
        return False
    return _looks_like_customer_self_report_fragment(normalized) or _looks_like_customer_answer_fragment(normalized)


def _looks_like_badge_owner_self_intro_fragment(text: str, staff_name: str | None) -> bool:
    compact = _compact_fragment_text(text)
    if not compact:
        return False

    normalized_name = re.sub(r"\s+", "", _clean_text(staff_name))
    if normalized_name and normalized_name in compact and re.search(
        rf"(?:我是|我叫){re.escape(normalized_name)}(?:老师|顾问|医生|设计师|咨询师|助理|护士)?",
        compact,
    ):
        return True

    role_descriptor = (
        r"(?:医生助理|医助|护士|咨询师|咨询顾问|美学顾问|美学设计师|设计师|顾问|"
        r"负责接待|现场接待|接待你|接待您|服务你|服务您)"
    )
    if re.search(rf"(?:我是|我叫|我这边是|我负责|由我负责|我来负责).{{0,18}}{role_descriptor}", compact):
        return True
    if re.search(rf"{role_descriptor}.{{0,18}}(?:是我|我来|我负责|我接待)", compact):
        return True
    return False


def _looks_like_short_staff_followup_after_intro(text: str) -> bool:
    compact = _compact_fragment_text(text)
    if not compact or len(compact) > 20:
        return False
    return compact in {
        "我看一下",
        "我帮你看一下",
        "我帮您看一下",
        "我给你看一下",
        "我给您看一下",
        "我这边看一下",
        "我先看一下",
    }


def _pick_resolved_role_key(utterances: list[dict], role: str) -> str | None:
    duration_by_key: dict[str, int] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        if _normalize_role(utterance.get("speaker")) != role:
            continue
        key = _speaker_key(utterance)
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        duration_by_key[key] = duration_by_key.get(key, 0) + max(end_ms - begin_ms, 300)
    if not duration_by_key:
        return None
    return max(duration_by_key.items(), key=lambda item: item[1])[0]


def _split_mixed_customer_turns(
    utterances: list[dict],
    *,
    staff_name: str | None,
) -> list[dict]:
    if len({_speaker_key(item) for item in utterances if isinstance(item, dict)}) < 2:
        return utterances

    badge_owner_key = _pick_resolved_role_key(utterances, "consultant") or _choose_staff_speaker(
        _collect_stats(utterances, staff_name=staff_name),
        staff_name=staff_name,
    )
    customer_key = _pick_resolved_role_key(utterances, "customer")
    if not badge_owner_key or not customer_key or badge_owner_key == customer_key:
        return utterances

    refined: list[dict] = []
    changed = False
    for utterance in utterances:
        if not isinstance(utterance, dict):
            refined.append(utterance)
            continue

        if _speaker_key(utterance) != badge_owner_key:
            refined.append(utterance)
            continue

        text = _clean_text(utterance.get("text"))
        if not _should_split_badge_owner_utterance(text):
            if _looks_like_standalone_customer_turn(text):
                copied = dict(utterance)
                copied["speaker"] = customer_key
                copied["speaker_id"] = customer_key
                for field in _ROLE_RESET_FIELDS:
                    copied.pop(field, None)
                refined.append(copied)
                changed = True
                continue
            refined.append(utterance)
            continue

        clauses = _split_text_clauses(text)
        assignments: list[tuple[str, str]] = []
        pending_customer_response = False
        badge_owner_intro_context = False
        for index, clause in enumerate(clauses):
            normalized_clause = _normalize_fragment_text(clause)
            if not normalized_clause:
                continue
            prev_clause = clauses[index - 1] if index > 0 else ""
            next_clause = clauses[index + 1] if index + 1 < len(clauses) else ""
            if (
                _looks_like_badge_owner_self_intro_fragment(normalized_clause, staff_name)
                or (
                    re.fullmatch(r"我(?:叫|是).{1,12}", _compact_fragment_text(normalized_clause))
                    and _looks_like_badge_owner_self_intro_fragment(next_clause, staff_name)
                )
                or (
                    badge_owner_intro_context
                    and (
                        _looks_like_short_staff_followup_after_intro(normalized_clause)
                        or _looks_like_staff_statement_fragment(normalized_clause)
                        or _looks_like_customer_question_fragment(normalized_clause)
                    )
                )
            ):
                assignments.append((badge_owner_key, normalized_clause))
                badge_owner_intro_context = True
                pending_customer_response = False
                continue
            customer_self_report = _looks_like_customer_self_report_fragment(normalized_clause)
            if _looks_like_customer_question_fragment(normalized_clause):
                assignments.append((badge_owner_key, normalized_clause))
                pending_customer_response = True
                continue
            if pending_customer_response and (
                _looks_like_customer_answer_fragment(normalized_clause) or customer_self_report
            ):
                assignments.append((customer_key, normalized_clause))
                pending_customer_response = False
                continue
            if customer_self_report and (
                _looks_like_customer_question_fragment(prev_clause)
                or _looks_like_staff_statement_fragment(next_clause)
                or (index > 0 and _looks_like_staff_statement_fragment(prev_clause))
            ):
                assignments.append((customer_key, normalized_clause))
                pending_customer_response = False
                continue
            if (
                _looks_like_customer_answer_fragment(normalized_clause)
                and (
                    _looks_like_staff_statement_fragment(next_clause)
                    or (index > 0 and _looks_like_staff_statement_fragment(prev_clause))
                )
            ):
                assignments.append((customer_key, normalized_clause))
                pending_customer_response = False
                continue
            if _looks_like_customer_answer_fragment(normalized_clause):
                assignments.append((customer_key, normalized_clause))
                pending_customer_response = False
                continue
            assignments.append((badge_owner_key, normalized_clause))
            pending_customer_response = False

        if len(assignments) <= 1 or not any(key == customer_key for key, _ in assignments):
            refined.append(utterance)
            continue

        changed = True
        timed_fragments = _timed_fragments(
            [fragment for _, fragment in assignments],
            int(utterance.get("begin_ms") or 0),
            int(utterance.get("end_ms") or utterance.get("begin_ms") or 0),
        )
        for (speaker_key, fragment), (_, fragment_begin, fragment_end) in zip(assignments, timed_fragments, strict=False):
            copied = dict(utterance)
            copied["speaker_id"] = speaker_key
            copied["text"] = fragment
            copied["begin_ms"] = fragment_begin
            copied["end_ms"] = fragment_end
            for field in _ROLE_RESET_FIELDS:
                copied.pop(field, None)
            assigned_role = "consultant" if speaker_key == badge_owner_key else "customer"
            copied["speaker"] = assigned_role
            copied["speaker_role"] = assigned_role
            copied["speaker_role_source"] = "mixed_turn_split"
            refined.append(copied)

    return refined if changed else utterances


def _staff_name_signal_hits(text: str, staff_name: str | None) -> int:
    normalized = re.sub(r"\s+", "", _clean_text(staff_name))
    if not normalized:
        return 0

    hits = 0
    if re.search(rf"(?:我是|我叫){re.escape(normalized)}(?:老师|顾问|医生)?", text):
        hits += 2

    surname = normalized[:1]
    if surname and re.search(rf"(?:我姓|您叫我|你叫我){re.escape(surname)}", text):
        hits += 1

    return hits


def _compact_text(value: object) -> str:
    return re.sub(r"\s+", "", _clean_text(value))


def _looks_like_explicit_badge_owner_intro(text: str, staff_name: str | None) -> bool:
    compact = _compact_text(text)
    normalized_name = _compact_text(staff_name)
    if not compact or not normalized_name or normalized_name not in compact:
        return False

    escaped_name = re.escape(normalized_name)
    if re.search(rf"(?:我是|我叫){escaped_name}(?:老师|顾问|医生|设计师|咨询师)?", compact):
        return True

    if not any(hint in compact for hint in _STAFF_SELF_INTRO_ROLE_HINTS):
        return False

    staff_role_descriptor = r"(?:负责|接待|服务|美学设计师|美学顾问|咨询师|咨询顾问|顾问|医生|院长|护士)"
    return bool(
        re.search(rf"(?:我是|我叫).{{0,36}}{staff_role_descriptor}.{{0,16}}{escaped_name}", compact)
        or re.search(rf"(?:我负责|由我负责|我来负责).{{0,36}}{escaped_name}", compact)
        or re.search(rf"(?:接待|服务).{{0,12}}(?:你|您).{{0,24}}{escaped_name}", compact)
        or re.search(rf"(?:美学设计师|美学顾问|咨询师|咨询顾问|顾问|医生|院长|护士).{{0,12}}{escaped_name}", compact)
    )


def _looks_like_staff_service_followup(text: str) -> bool:
    compact = _compact_text(text)
    return bool(compact and any(hint in compact for hint in _STAFF_SERVICE_FOLLOWUP_HINTS))


def _mark_as_explicit_badge_owner(
    utterance: dict,
    *,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
    source: str,
) -> None:
    owner_role = _normalize_staff_role(staff_role)
    normalized_staff_id = _clean_text(staff_id)
    normalized_staff_name = _clean_text(staff_name)

    utterance["speaker"] = owner_role
    utterance["speaker_role"] = owner_role
    utterance["speaker_role_source"] = source
    utterance["speaker_identity_type"] = "staff"
    utterance["speaker_business_role"] = "badge_owner"
    if normalized_staff_id:
        utterance["speaker_staff_id"] = normalized_staff_id
    if normalized_staff_name:
        utterance["speaker_staff_name"] = normalized_staff_name
    utterance["speaker_display_label"] = _display_label_for_business_role(
        "badge_owner",
        speaker_id=_speaker_key(utterance),
        owner_staff_name=staff_name,
        matched_staff_name=normalized_staff_name,
    )


def _mark_as_content_staff(
    utterance: dict,
    *,
    business_role: str,
    label: str,
    source: str,
) -> None:
    coarse_role = BUSINESS_ROLE_TO_COARSE_ROLE.get(business_role, "consultant")
    utterance["speaker"] = coarse_role
    utterance["speaker_role"] = coarse_role
    utterance["speaker_role_source"] = source
    utterance["speaker_identity_type"] = "staff"
    utterance["speaker_business_role"] = business_role
    utterance["speaker_display_label"] = label
    if business_role != "badge_owner":
        utterance.pop("speaker_staff_id", None)
        utterance.pop("speaker_staff_name", None)


def _content_override_keys(utterance: dict) -> set[str]:
    keys = {_speaker_key(utterance)}
    for field in ("asr_original_speaker_id", "asr_original_speaker"):
        value = _clean_text(utterance.get(field))
        if value:
            keys.add(value)
    return {key for key in keys if key and key != "unknown"}


def _looks_like_non_doctor_staff_content(text: str) -> bool:
    compact = _compact_text(text)
    if not compact:
        return False
    if any(hint in compact for hint in _NON_DOCTOR_STAFF_IDENTITY_HINTS):
        return True
    return any(hint in compact for hint in _NON_DOCTOR_STAFF_DELEGATION_HINTS)


def _doctor_explanation_score(text: str) -> tuple[int, int, bool]:
    compact = _compact_text(text)
    medical_hits = sum(1 for hint in _DOCTOR_EXPLANATION_MEDICAL_HINTS if hint in compact)
    flow_hits = sum(1 for hint in _DOCTOR_EXPLANATION_FLOW_HINTS if hint in compact)
    has_address = any(hint in compact for hint in _STAFF_EXPLANATION_ADDRESS_HINTS)
    return medical_hits, flow_hits, has_address


def _looks_like_doctor_explanation_mislabeled_as_customer(text: str) -> bool:
    compact = _compact_text(text)
    if len(compact) < 18:
        return False
    medical_hits, flow_hits, has_address = _doctor_explanation_score(text)
    if not has_address:
        return False
    if medical_hits >= 2 and flow_hits >= 1:
        return True
    return medical_hits >= 3 and len(compact) >= 36


def _looks_like_staff_explanation_context(text: str) -> bool:
    compact = _compact_text(text)
    if len(compact) < 3:
        return False
    if any(hint in compact for hint in _STAFF_EXPLANATION_FOLLOWUP_HINTS):
        return True
    medical_hits, flow_hits, has_address = _doctor_explanation_score(text)
    return has_address and (medical_hits >= 1 or flow_hits >= 1)


def _looks_like_non_doctor_staff_context(text: str) -> bool:
    compact = _compact_text(text)
    if not compact:
        return False
    if len(compact) <= 18 and not _looks_like_customer_treatment_history_self_report(compact):
        return True
    return any(hint in compact for hint in _NON_DOCTOR_STAFF_CONTEXT_HINTS)


def _apply_content_role_overrides(utterances: list[dict]) -> list[dict]:
    active_staff_until_by_key: dict[str, int] = {}
    active_doctor_until_by_key: dict[str, int] = {}

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue

        text = _clean_text(utterance.get("text"))
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        keys = _content_override_keys(utterance)
        speaker_key = _speaker_key(utterance)
        current_business_role = _clean_text(utterance.get("speaker_business_role"))
        if current_business_role == "badge_owner":
            continue
        for key in list(active_staff_until_by_key):
            if active_staff_until_by_key[key] < begin_ms:
                active_staff_until_by_key.pop(key, None)
        for key in list(active_doctor_until_by_key):
            if active_doctor_until_by_key[key] < begin_ms:
                active_doctor_until_by_key.pop(key, None)

        if _looks_like_non_doctor_staff_content(text):
            _mark_as_content_staff(
                utterance,
                business_role="staff_peer",
                label="专家助理" if "专家助理" in _compact_text(text) else "员工同事",
                source="content_non_doctor_staff",
            )
            for key in keys:
                active_staff_until_by_key[key] = end_ms + 90_000
            continue

        if _looks_like_doctor_explanation_mislabeled_as_customer(text):
            _mark_as_content_staff(
                utterance,
                business_role="doctor",
                label="医生",
                source="content_doctor_explanation",
            )
            for key in keys:
                active_doctor_until_by_key[key] = end_ms + 45_000
            continue

        active_staff_keys = keys & set(active_staff_until_by_key)
        active_staff_by_speaker = speaker_key in active_staff_until_by_key
        active_staff_by_raw_only = bool(active_staff_keys) and not active_staff_by_speaker
        if (
            keys
            and active_staff_keys
            and (
                _looks_like_non_doctor_staff_context(text)
                if active_staff_by_speaker
                else any(hint in _compact_text(text) for hint in _NON_DOCTOR_STAFF_CONTEXT_HINTS)
                or (not active_staff_by_raw_only and _looks_like_non_doctor_staff_context(text))
            )
        ):
            _mark_as_content_staff(
                utterance,
                business_role="staff_peer",
                label="员工同事",
                source="content_staff_explanation_context",
            )
            for key in keys:
                active_staff_until_by_key[key] = end_ms + 45_000
            continue

        if keys and _looks_like_staff_explanation_context(text):
            if any(key in active_doctor_until_by_key for key in keys):
                _mark_as_content_staff(
                    utterance,
                    business_role="doctor",
                    label="医生",
                    source="content_staff_explanation_context",
                )
                for key in keys:
                    active_doctor_until_by_key[key] = end_ms + 45_000
                continue
            if any(key in active_staff_until_by_key for key in keys):
                _mark_as_content_staff(
                    utterance,
                    business_role="staff_peer",
                    label="员工同事",
                    source="content_staff_explanation_context",
                )
                for key in keys:
                    active_staff_until_by_key[key] = end_ms + 45_000
                continue

    return utterances


def _looks_like_customer_treatment_history_self_report(text: str) -> bool:
    compact = _compact_text(text)
    if not compact:
        return False

    part_pattern = "|".join(re.escape(item) for item in _CUSTOMER_TREATMENT_HISTORY_PARTS)
    verb_pattern = "|".join(re.escape(item) for item in _CUSTOMER_TREATMENT_HISTORY_VERBS)
    mention_pattern = rf"我(?:的)?(?:{part_pattern}).{{0,8}}(?:{verb_pattern})"
    mentions = re.findall(mention_pattern, compact)
    if len(mentions) >= 2:
        return True

    return bool(
        re.search(r"我全脸都(?:动过|打过|做过)", compact)
        or re.search(r"我脸上花了好多钱", compact)
        or re.search(r"我.{0,40}(?:打了瘦脸针|瘦脸针之后)", compact)
        or re.search(r"我.{0,30}(?:打过|打了|填过|注射过).{0,8}玻尿酸", compact)
    )


def _looks_like_customer_treatment_history_followup(text: str) -> bool:
    compact = _compact_text(text)
    return bool(compact and any(hint in compact for hint in _CUSTOMER_TREATMENT_HISTORY_FOLLOWUP_HINTS))


def _mark_as_explicit_primary_customer(utterance: dict, *, source: str) -> None:
    utterance["speaker"] = "customer"
    utterance["speaker_role"] = "customer"
    utterance["speaker_role_source"] = source
    utterance["speaker_identity_type"] = "visitor"
    utterance["speaker_business_role"] = "primary_customer"
    utterance["speaker_display_label"] = _display_label_for_business_role(
        "primary_customer",
        speaker_id=_speaker_key(utterance),
        owner_staff_name=None,
        matched_staff_name=None,
    )
    utterance.pop("speaker_staff_id", None)
    utterance.pop("speaker_staff_name", None)


def _apply_explicit_primary_customer_overrides(utterances: list[dict]) -> list[dict]:
    active_speaker_key: str | None = None
    active_until_ms = -1
    followup_count = 0

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue

        text = _clean_text(utterance.get("text"))
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        speaker_key = _speaker_key(utterance)
        source = _clean_text(utterance.get("speaker_role_source"))

        if source in _EXPLICIT_BADGE_OWNER_SOURCES:
            active_speaker_key = None
            active_until_ms = -1
            followup_count = 0
            continue

        if _looks_like_customer_treatment_history_self_report(text):
            _mark_as_explicit_primary_customer(
                utterance,
                source="explicit_customer_treatment_history",
            )
            active_speaker_key = speaker_key
            active_until_ms = end_ms + 30_000
            followup_count = 0
            continue

        if (
            active_speaker_key
            and speaker_key == active_speaker_key
            and begin_ms <= active_until_ms
            and followup_count < 3
            and _looks_like_customer_treatment_history_followup(text)
        ):
            _mark_as_explicit_primary_customer(
                utterance,
                source="explicit_customer_treatment_history_context",
            )
            active_until_ms = end_ms + 20_000
            followup_count += 1
            continue

        if begin_ms > active_until_ms or speaker_key != active_speaker_key:
            active_speaker_key = None
            active_until_ms = -1
            followup_count = 0

    return utterances


def _apply_explicit_badge_owner_overrides(
    utterances: list[dict],
    *,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
) -> list[dict]:
    active_speaker_key: str | None = None
    owner_speaker_keys: set[str] = set()
    active_until_ms = -1
    followup_count = 0

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue

        text = _clean_text(utterance.get("text"))
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        speaker_key = _speaker_key(utterance)

        if _looks_like_explicit_badge_owner_intro(text, staff_name):
            _mark_as_explicit_badge_owner(
                utterance,
                staff_id=staff_id,
                staff_name=staff_name,
                staff_role=staff_role,
                source="explicit_staff_intro",
            )
            owner_speaker_keys.add(speaker_key)
            active_speaker_key = speaker_key
            active_until_ms = end_ms + 7_000
            followup_count = 0
            continue

        if (
            active_speaker_key
            and speaker_key == active_speaker_key
            and begin_ms <= active_until_ms
            and followup_count < 2
            and _looks_like_staff_service_followup(text)
        ):
            _mark_as_explicit_badge_owner(
                utterance,
                staff_id=staff_id,
                staff_name=staff_name,
                staff_role=staff_role,
                source="explicit_staff_intro_context",
            )
            active_until_ms = end_ms + 3_000
            followup_count += 1
            continue

        if (
            speaker_key in owner_speaker_keys
            and _looks_like_staff_statement_fragment(text)
            and not _looks_like_customer_treatment_history_self_report(text)
            and not _looks_like_customer_treatment_history_followup(text)
        ):
            _mark_as_explicit_badge_owner(
                utterance,
                staff_id=staff_id,
                staff_name=staff_name,
                staff_role=staff_role,
                source="explicit_staff_intro_context",
            )
            continue

        if begin_ms > active_until_ms or speaker_key != active_speaker_key:
            active_speaker_key = None
            active_until_ms = -1
            followup_count = 0

    return utterances


def _collect_stats(utterances: list[dict], *, staff_name: str | None) -> dict[str, _SpeakerStats]:
    stats_by_key: dict[str, _SpeakerStats] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        if _clean_text(utterance.get("speaker_role_source")) in _PER_UTTERANCE_ROLE_SOURCES:
            continue
        text = _clean_text(utterance.get("text"))
        if not text:
            continue

        key = _speaker_key(utterance)
        begin_ms = int(utterance.get("begin_ms") or 0)
        end_ms = int(utterance.get("end_ms") or begin_ms)
        duration_ms = max(end_ms - begin_ms, 300)

        stats = stats_by_key.setdefault(key, _SpeakerStats(key=key, first_begin_ms=begin_ms))
        stats.first_begin_ms = min(stats.first_begin_ms, begin_ms)
        stats.utterance_count += 1
        stats.total_duration_ms += duration_ms
        stats.total_chars += len(text)
        stats.question_hits += _count_hits(text, _QUESTION_PATTERNS)
        stats.staff_keyword_hits += _count_hits(text, _STAFF_INTRO_PATTERNS)
        stats.customer_keyword_hits += _count_hits(text, _CUSTOMER_PATTERNS)
        stats.companion_keyword_hits += _count_hits(text, _COMPANION_PATTERNS)
        stats.doctor_keyword_hits += _count_hits(text, _DOCTOR_PATTERNS)
        stats.generic_staff_intro_hits += _count_hits(text, _GENERIC_STAFF_INTRO_PATTERNS)
        stats.staff_name_hits += _staff_name_signal_hits(text, staff_name)
        stats.polite_you_hits += text.count("您") + text.count("咱")
        stats.self_reference_hits += text.count("我")

    return stats_by_key


def _existing_role_mapping(utterances: list[dict]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        source = _clean_text(utterance.get("speaker_role_source"))
        if source in _PER_UTTERANCE_ROLE_SOURCES or source in _STALE_HEURISTIC_ROLE_SOURCES:
            continue
        role = _normalize_role(utterance.get("speaker"))
        if role == "unknown":
            continue
        mapping.setdefault(_speaker_key(utterance), role)
    return mapping


def _choose_staff_speaker(
    stats_by_key: dict[str, _SpeakerStats],
    *,
    staff_name: str | None,
) -> str | None:
    if not stats_by_key:
        return None

    threshold = 2.0 if not staff_name else 2.5
    candidates: list[_SpeakerStats] = []
    for item in stats_by_key.values():
        has_explicit_staff_identity = item.staff_name_hits > 0 or item.generic_staff_intro_hits > 0
        if has_explicit_staff_identity:
            candidates.append(item)
            continue
        if item.staff_score < threshold:
            continue
        # Customer-heavy speakers can still ask many questions and speak for a
        # long time, which inflates staff_score. Do not select them as the badge
        # owner unless there is an explicit staff identity signal.
        if item.customer_score >= item.staff_score + 1.0 and item.customer_keyword_hits >= item.staff_keyword_hits:
            continue
        if item.staff_score < item.customer_score + 1.0:
            continue
        candidates.append(item)
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (
            item.staff_name_hits,
            item.generic_staff_intro_hits,
            item.staff_score - item.customer_score,
            item.staff_score,
            item.total_duration_ms,
            -item.first_begin_ms,
        ),
    )
    if best.staff_name_hits > 0 or best.generic_staff_intro_hits > 0 or best.staff_score >= threshold:
        return best.key
    return None


def _choose_customer_speaker(
    stats_by_key: dict[str, _SpeakerStats],
    *,
    staff_key: str | None,
) -> str | None:
    remaining = [item for item in stats_by_key.values() if item.key != staff_key]
    if not remaining:
        return None
    if staff_key and len(stats_by_key) == 2:
        return remaining[0].key

    candidate = max(
        remaining,
        key=lambda item: (
            item.customer_score,
            -item.staff_score,
            -item.first_begin_ms,
        ),
    )
    if candidate.customer_score >= 1.5:
        return candidate.key
    return None


def _choose_doctor_speaker(
    stats_by_key: dict[str, _SpeakerStats],
    *,
    excluded_keys: set[str],
) -> str | None:
    remaining = [item for item in stats_by_key.values() if item.key not in excluded_keys]
    if not remaining:
        return None
    candidate = max(remaining, key=lambda item: (item.doctor_score, item.total_duration_ms, -item.first_begin_ms))
    if candidate.doctor_score >= 2.5:
        return candidate.key
    return None


def _choose_doctor_business_speaker(
    stats_by_key: dict[str, _SpeakerStats],
    *,
    excluded_keys: set[str],
) -> str | None:
    candidates = [item for item in stats_by_key.values() if item.key not in excluded_keys]
    if not candidates:
        return None

    candidate = max(
        candidates,
        key=lambda item: (
            item.doctor_score,
            item.doctor_keyword_hits,
            -item.first_begin_ms,
            item.total_duration_ms,
        ),
    )
    if candidate.doctor_keyword_hits >= 2 and candidate.doctor_score >= 4.0:
        return candidate.key
    if candidate.doctor_keyword_hits >= 4:
        return candidate.key
    return None


def _infer_roles(
    utterances: list[dict],
    *,
    staff_name: str | None,
    staff_role: str | None,
) -> dict[str, str]:
    mapping = _existing_role_mapping(utterances)
    if mapping and all(not _is_raw_speaker_label(key) for key in mapping):
        return mapping

    stats_by_key = _collect_stats(utterances, staff_name=staff_name)
    if not stats_by_key:
        return mapping

    employee_key = _choose_staff_speaker(stats_by_key, staff_name=staff_name)
    employee_role = _normalize_staff_role(staff_role)
    if employee_key and employee_key not in mapping:
        mapping[employee_key] = employee_role

    customer_key = _choose_customer_speaker(stats_by_key, staff_key=employee_key)
    if customer_key and customer_key not in mapping:
        mapping[customer_key] = "customer"

    if len(stats_by_key) >= 3:
        doctor_key = _choose_doctor_speaker(stats_by_key, excluded_keys=set(mapping))
        if doctor_key and doctor_key not in mapping:
            mapping[doctor_key] = "doctor"

    return mapping


def _speaker_duration_by_key(stats_by_key: dict[str, _SpeakerStats]) -> dict[str, int]:
    return {
        key: item.total_duration_ms
        for key, item in stats_by_key.items()
    }


def _choose_primary_customer(
    stats_by_key: dict[str, _SpeakerStats],
    visitor_keys: set[str],
) -> str | None:
    if not visitor_keys:
        return None
    candidates = [stats_by_key[key] for key in visitor_keys if key in stats_by_key]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (
            item.primary_customer_score,
            -item.companion_score,
            item.customer_score,
            item.total_duration_ms,
            item.utterance_count,
            -item.first_begin_ms,
        ),
    )
    return best.key


def _choose_badge_owner_from_semantic_roles(
    stats_by_key: dict[str, _SpeakerStats],
    semantic_roles_by_key: dict[str, set[str]],
) -> str | None:
    candidates = [
        stats
        for key, stats in stats_by_key.items()
        if "consultant" in semantic_roles_by_key.get(key, set()) and "customer" not in semantic_roles_by_key.get(key, set())
    ]
    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (
            item.staff_name_hits,
            item.staff_score,
            item.total_duration_ms,
            -item.first_begin_ms,
        ),
    )
    return best.key


def _display_label_for_business_role(
    business_role: str,
    *,
    speaker_id: str,
    owner_staff_name: str | None,
    matched_staff_name: str | None,
) -> str:
    normalized_owner_name = _clean_text(owner_staff_name)
    normalized_matched_name = _clean_text(matched_staff_name)
    if business_role == "badge_owner" and normalized_owner_name:
        return f"{normalized_owner_name}（工牌本人）"
    if business_role == "staff_peer" and normalized_matched_name:
        return f"{normalized_matched_name}（员工同事）"
    if business_role == "doctor" and normalized_matched_name:
        return f"{normalized_matched_name}（医生）"
    if business_role in BUSINESS_ROLE_LABELS:
        return BUSINESS_ROLE_LABELS[business_role]
    return speaker_id or BUSINESS_ROLE_LABELS["unknown"]


def _normalize_business_role(value: object) -> str | None:
    role = _clean_text(value)
    if role in BUSINESS_ROLE_LABELS and role != "unknown":
        return role
    return None


def _identity_type_for_business_role(role: str) -> str:
    if role in {"badge_owner", "staff_peer", "doctor"}:
        return "staff"
    if role in {"primary_customer", "visitor_companion"}:
        return "visitor"
    return "unknown"


def _annotate_speaker_taxonomy(
    utterances: list[dict],
    *,
    staff_id: str | None,
    staff_name: str | None,
    staff_role: str | None,
) -> list[dict]:
    if not utterances:
        return utterances

    stats_by_key = _collect_stats(utterances, staff_name=staff_name)
    if not stats_by_key:
        return utterances

    semantic_roles_by_key = _speaker_semantic_roles(utterances)
    staff_ids_by_key = _speaker_group_values(utterances, "speaker_staff_id")
    staff_names_by_key = _speaker_group_values(utterances, "speaker_staff_name")
    explicit_business_roles: dict[str, str] = {}
    explicit_role_sets: dict[str, set[str]] = {}
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        role = _normalize_business_role(utterance.get("speaker_business_role"))
        if role is None:
            continue
        explicit_role_sets.setdefault(_speaker_key(utterance), set()).add(role)
    for speaker_key, roles in explicit_role_sets.items():
        if len(roles) == 1:
            explicit_business_roles[speaker_key] = next(iter(roles))

    normalized_staff_id = _clean_text(staff_id)
    speaker_business_roles: dict[str, str] = {}
    speaker_identity_types: dict[str, str] = {}

    badge_owner_key: str | None = None
    for speaker_key, role in explicit_business_roles.items():
        if role == "badge_owner":
            badge_owner_key = speaker_key
            break
    if normalized_staff_id:
        for speaker_key, values in staff_ids_by_key.items():
            if normalized_staff_id in values:
                badge_owner_key = speaker_key
                break
    if not badge_owner_key:
        badge_owner_key = _choose_badge_owner_from_semantic_roles(stats_by_key, semantic_roles_by_key)
    if not badge_owner_key:
        badge_owner_key = _choose_staff_speaker(stats_by_key, staff_name=staff_name)

    staff_like_keys: set[str] = set()
    visitor_keys: set[str] = set()
    unknown_keys: set[str] = set()
    for speaker_key, stats in stats_by_key.items():
        semantic_roles = semantic_roles_by_key.get(speaker_key, set())
        if speaker_key == badge_owner_key:
            staff_like_keys.add(speaker_key)
            continue
        if semantic_roles & STAFF_ROLES:
            staff_like_keys.add(speaker_key)
            continue
        if semantic_roles & VISITOR_ROLES:
            visitor_keys.add(speaker_key)
            continue
        if stats.customer_score >= max(stats.staff_score + 1.5, stats.doctor_score + 1.5, 2.0):
            visitor_keys.add(speaker_key)
            continue
        if stats.doctor_score >= max(stats.customer_score + 2.5, stats.staff_score + 2.0, 6.0):
            staff_like_keys.add(speaker_key)
            continue
        if stats.staff_score >= max(stats.customer_score + 1.5, 4.0):
            staff_like_keys.add(speaker_key)
            continue
        if stats.customer_score >= 1.2:
            visitor_keys.add(speaker_key)
            continue
        unknown_keys.add(speaker_key)

    primary_customer_key = _choose_primary_customer(stats_by_key, visitor_keys)
    speaker_duration = _speaker_duration_by_key(stats_by_key)
    doctor_key = _choose_doctor_business_speaker(
        stats_by_key,
        excluded_keys=set(visitor_keys) | ({badge_owner_key} if badge_owner_key else set()),
    )

    for speaker_key in stats_by_key:
        semantic_roles = semantic_roles_by_key.get(speaker_key, set())
        matched_staff_name = next(iter(staff_names_by_key.get(speaker_key, set())), None)
        explicit_business_role = explicit_business_roles.get(speaker_key)
        if explicit_business_role:
            business_role = explicit_business_role
            identity_type = _identity_type_for_business_role(business_role)
        elif speaker_key == badge_owner_key:
            business_role = "badge_owner"
            identity_type = "staff"
        elif speaker_key == doctor_key:
            business_role = "doctor"
            identity_type = "staff"
        elif speaker_key in staff_like_keys:
            business_role = "doctor" if "doctor" in semantic_roles else "staff_peer"
            identity_type = "staff"
        elif speaker_key in visitor_keys:
            business_role = "primary_customer" if speaker_key == primary_customer_key else "visitor_companion"
            identity_type = "visitor"
        elif speaker_key in unknown_keys:
            business_role = "unknown"
            identity_type = "unknown"
        else:
            business_role = "unknown"
            identity_type = "unknown"

        # If a raw speaker got staff voiceprint binding but no explicit semantic role, still keep it on the staff side.
        if matched_staff_name and business_role in {"unknown", "visitor_companion", "primary_customer"}:
            business_role = "doctor" if "doctor" in semantic_roles else "staff_peer"
            identity_type = "staff"

        # If the dominant non-staff speaker remains unknown, surface it as the primary customer for multi-party sessions.
        if (
            business_role == "unknown"
            and not matched_staff_name
            and speaker_duration.get(speaker_key, 0) > 15_000
            and primary_customer_key is None
        ):
            business_role = "primary_customer"
            identity_type = "visitor"
            primary_customer_key = speaker_key

        if (
            business_role == "unknown"
            and primary_customer_key
            and speaker_key != primary_customer_key
            and speaker_key not in staff_like_keys
        ):
            business_role = "visitor_companion"
            identity_type = "visitor"

        speaker_business_roles[speaker_key] = business_role
        speaker_identity_types[speaker_key] = identity_type

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        source = _clean_text(utterance.get("speaker_role_source"))
        if source in _EXPLICIT_BADGE_OWNER_SOURCES:
            owner_role = _normalize_staff_role(staff_role)
            normalized_staff_id = _clean_text(staff_id)
            normalized_staff_name = _clean_text(staff_name)
            if normalized_staff_id:
                utterance["speaker_staff_id"] = normalized_staff_id
            if normalized_staff_name:
                utterance["speaker_staff_name"] = normalized_staff_name
            utterance["speaker"] = owner_role
            utterance["speaker_role"] = owner_role
            utterance["speaker_identity_type"] = "staff"
            utterance["speaker_business_role"] = "badge_owner"
            utterance["speaker_display_label"] = _display_label_for_business_role(
                "badge_owner",
                speaker_id=_speaker_key(utterance),
                owner_staff_name=staff_name,
                matched_staff_name=normalized_staff_name,
            )
            continue
        if source in _EXPLICIT_PRIMARY_CUSTOMER_SOURCES:
            utterance["speaker"] = "customer"
            utterance["speaker_role"] = "customer"
            utterance["speaker_identity_type"] = "visitor"
            utterance["speaker_business_role"] = "primary_customer"
            utterance["speaker_display_label"] = _display_label_for_business_role(
                "primary_customer",
                speaker_id=_speaker_key(utterance),
                owner_staff_name=staff_name,
                matched_staff_name=None,
            )
            utterance.pop("speaker_staff_id", None)
            utterance.pop("speaker_staff_name", None)
            continue

        speaker_key = _speaker_key(utterance)
        business_role = speaker_business_roles.get(speaker_key, "unknown")
        identity_type = speaker_identity_types.get(speaker_key, "unknown")
        coarse_role = BUSINESS_ROLE_TO_COARSE_ROLE.get(business_role)
        matched_staff_name = _clean_text(utterance.get("speaker_staff_name"))

        utterance["speaker_identity_type"] = identity_type
        utterance["speaker_business_role"] = business_role
        utterance["speaker_display_label"] = _display_label_for_business_role(
            business_role,
            speaker_id=speaker_key,
            owner_staff_name=staff_name,
            matched_staff_name=matched_staff_name,
        )
        if coarse_role:
            utterance.setdefault("speaker_role", coarse_role)

    return _apply_content_role_overrides(utterances)


def resolve_speaker_roles(
    utterances: list[dict],
    *,
    staff_id: str | None = None,
    staff_name: str | None = None,
    staff_role: str | None = None,
    respect_speaker_diarization: bool = False,
    split_mixed_turns: bool | None = None,
) -> list[dict]:
    if not utterances:
        return _annotate_speaker_taxonomy(
            utterances,
            staff_id=staff_id,
            staff_name=staff_name,
            staff_role=staff_role,
        )

    def _apply_mapping(target_utterances: list[dict], mapping: dict[str, str]) -> list[dict]:
        if not mapping:
            for utterance in target_utterances:
                if not isinstance(utterance, dict):
                    continue
                current_role = _normalize_role(utterance.get("speaker"))
                if current_role != "unknown":
                    utterance["speaker"] = current_role
                    utterance.setdefault("speaker_role", current_role)
                    utterance.setdefault("speaker_role_source", "upstream")
            return target_utterances

        for utterance in target_utterances:
            if not isinstance(utterance, dict):
                continue

            speaker_key = _speaker_key(utterance)
            resolved_role = mapping.get(speaker_key)
            current_role = _normalize_role(utterance.get("speaker"))
            source = _clean_text(utterance.get("speaker_role_source"))

            if (
                current_role != "unknown"
                and not _is_raw_speaker_label(utterance.get("speaker"))
                and source not in _STALE_HEURISTIC_ROLE_SOURCES
            ):
                utterance["speaker"] = current_role
                utterance.setdefault("speaker_role", current_role)
                utterance.setdefault("speaker_role_source", "upstream")
                continue

            if resolved_role:
                utterance["speaker"] = resolved_role
                utterance["speaker_role"] = resolved_role
                utterance["speaker_role_source"] = "local_heuristic"
        return target_utterances

    mapping = _infer_roles(utterances, staff_name=staff_name, staff_role=staff_role)
    if not mapping:
        _apply_mapping(utterances, mapping)
        return utterances

    _apply_mapping(utterances, mapping)
    should_split_mixed_turns = (not respect_speaker_diarization) if split_mixed_turns is None else split_mixed_turns
    if should_split_mixed_turns:
        utterances = _split_mixed_customer_turns(utterances, staff_name=staff_name)
    mapping = _infer_roles(utterances, staff_name=staff_name, staff_role=staff_role)
    if not mapping:
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            current_role = _normalize_role(utterance.get("speaker"))
            if current_role != "unknown":
                utterance["speaker"] = current_role
                utterance.setdefault("speaker_role", current_role)
                utterance.setdefault("speaker_role_source", "upstream")
    else:
        _apply_mapping(utterances, mapping)

    _apply_explicit_badge_owner_overrides(
        utterances,
        staff_id=staff_id,
        staff_name=staff_name,
        staff_role=staff_role,
    )
    _apply_explicit_primary_customer_overrides(utterances)

    return _annotate_speaker_taxonomy(
        utterances,
        staff_id=staff_id,
        staff_name=staff_name,
        staff_role=staff_role,
    )
