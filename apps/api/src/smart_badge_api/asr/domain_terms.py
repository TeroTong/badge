from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from smart_badge_api.core.config import get_settings
from smart_badge_api.db.models import HotwordGroup
from smart_badge_api.db.session import _session_factory

logger = logging.getLogger(__name__)

_DEFAULT_HOTWORD_FILE = Path(__file__).resolve().parents[3] / "scripts" / "asr_hotwords_default.txt"
_TENCENT_MAX_HOTWORDS = 128
_TENCENT_MAX_HOTWORD_CHARS = 30
_DB_GROUP_PRIORITY = {
    "project": 0,
    "项目": 0,
    "brand": 1,
    "品牌": 1,
    "material": 1,
    "材料": 1,
    "industry": 1,
    "行业": 1,
    "通用": 2,
    "service": 2,
    "concern": 3,
    "顾虑": 3,
    "competitor": 4,
    "竞品": 4,
}
_TEXT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("蛮化", "馒化"),
    ("膜化", "馒化"),
    ("漫化", "馒化"),
    ("玻料酸", "玻尿酸"),
    ("波尿酸", "玻尿酸"),
    ("嗨替", "嗨体"),
    ("海体", "嗨体"),
    ("热马吉", "热玛吉"),
    ("热玛姬", "热玛吉"),
    ("超声刨", "超声炮"),
    ("超声泡", "超声炮"),
    ("超生炮", "超声炮"),
    ("超生泡", "超声炮"),
    ("英文大提升", "英伦大提升"),
    ("英文大提生", "英伦大提升"),
    ("英伦大提生", "英伦大提升"),
    ("英轮大提升", "英伦大提升"),
    ("如白天使", "濡白天使"),
    ("儒白天使", "濡白天使"),
    ("乳白天使", "濡白天使"),
    ("乳天使", "濡白天使"),
    ("宝妥适", "保妥适"),
    ("宝妥市", "保妥适"),
    ("保妥市", "保妥适"),
    ("宝妥式", "保妥适"),
    ("保妥式", "保妥适"),
    ("宝头市", "保妥适"),
    ("宝头式", "保妥适"),
    ("宝土石", "保妥适"),
    ("桥雅登", "乔雅登"),
    ("巧雅登", "乔雅登"),
    ("瑞兰", "瑞蓝"),
    ("爱丽薇", "艾莉薇"),
    ("艾丽薇", "艾莉薇"),
    ("鼻基地", "鼻基底"),
    ("苹果机", "苹果肌"),
    ("马甲现", "马甲线"),
    ("下颚缘", "下颌缘"),
    ("泪勾", "泪沟"),
    ("框隔", "眶隔"),
    ("矿隔", "眶隔"),
    ("颞曲", "颞区"),
    ("棚体", "膨体"),
    ("位期", "外切"),
    ("外戏", "外切"),
    ("提胜", "提升"),
    ("体肌", "提肌"),
    ("润制", "润致"),
    ("有劝", "有券"),
    ("大纲讲", "大概讲"),
    ("耳卵骨", "耳软骨"),
    ("勒软骨", "肋软骨"),
    ("夹壳", "结痂"),
    ("加咳", "结痂"),
    ("黄金位置", "黄金微针"),
    ("光子乘复", "光子嫩肤"),
    ("外戚眼袋", "外切眼袋"),
    ("乐体乐提葆", "乐提葆"),
    ("乐体宝", "乐提葆"),
    ("乐体葆", "乐提葆"),
    ("乐体堡", "乐提葆"),
    ("乐奇宝", "乐提葆"),
    ("乐起宝", "乐提葆"),
    ("瑞德仪", "瑞德喜"),
    ("菲林浮利", "菲林普利"),
    ("贝利菲尔", "贝丽菲尔"),
    ("菲利菲尔", "贝丽菲尔"),
    ("薇医美", "薇旖美"),
    ("微医美", "薇旖美"),
    ("维医美", "薇旖美"),
    ("我医美", "薇旖美"),
)
_PHRASE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"外[去戏期戚](眼袋|祛眼袋)"), r"外切\1"),
    (re.compile(r"内[去戏期戚](眼袋|祛眼袋)"), r"内切\1"),
    (re.compile(r"外[去戏期戚]眼代"), "外切眼袋"),
    (re.compile(r"内[去戏期戚]眼代"), "内切眼袋"),
    (re.compile(r"(外切|内切|祛)(眼代)"), r"\1眼袋"),
    (re.compile(r"轻度体积"), "轻度提肌"),
    (re.compile(r"眶隔脂肪(?:复位|位移)"), "眶隔脂肪复位"),
    (re.compile(r"[框矿]隔脂肪释放"), "眶隔脂肪释放"),
    (re.compile(r"(腰腹|腰部|腹部|大腿|手臂|面部|腰臀)(?:锡纸|锡脂|稀脂|西脂)"), r"\1吸脂"),
    (re.compile(r"(腰腹|腹部|大腿|手臂|面部)环[西锡细吸系膝]"), r"\1环吸"),
    (re.compile(r"润\s*no\s*v", re.IGNORECASE), "润诺威"),
)


@dataclass(slots=True)
class _HotwordEntry:
    term: str
    weight: int
    priority: int = 0


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _normalize_tencent_hotword_weight(weight: int) -> int:
    if weight >= 100:
        return 100
    if weight <= 1:
        return 1
    return min(((weight - 1) // 9) + 1, 11)


def _priority_for_group(group_type: str | None) -> int:
    return _DB_GROUP_PRIORITY.get(str(group_type or "").strip(), 5)


def _is_tencent_hotword_group_enabled(group: HotwordGroup) -> bool:
    group_type = str(group.group_type or "").strip()
    if group_type in {"project", "项目", "brand", "品牌", "material", "材料"}:
        return True

    descriptor = f"{group_type} {group.name or ''} {group.source_label or ''}".casefold()
    return any(marker in descriptor for marker in ("材料", "品牌", "material", "brand"))


def _effective_db_hotword_weight(weight: int, group_type: str | None) -> int:
    group_priority = _priority_for_group(group_type)
    if group_priority <= 1:
        # Project/material brand terms are the words most likely to be confused
        # by ASR, so keep them strong even when their UI weight is low.
        return max(weight, 80)
    return weight


def _load_default_hotword_entries() -> list[_HotwordEntry]:
    if not _DEFAULT_HOTWORD_FILE.exists():
        return []

    entries: list[_HotwordEntry] = []
    for index, raw_line in enumerate(_DEFAULT_HOTWORD_FILE.read_text(encoding="utf-8").splitlines()):
        term = _clean_term(raw_line)
        if not term or raw_line.strip().startswith("#"):
            continue
        if index < 24:
            weight = 11
        elif index < 56:
            weight = 10
        else:
            weight = 8
        # Managed DB hotwords should outrank this fallback list. Tencent only
        # accepts a limited HotwordList, so configured brand/project terms must
        # not be pushed out by generic defaults.
        entries.append(_HotwordEntry(term=term, weight=weight, priority=10))
    return entries


def _parse_manual_hotword_entries(value: str) -> list[_HotwordEntry]:
    entries: list[_HotwordEntry] = []
    for raw_part in str(value or "").split(","):
        part = raw_part.strip()
        if not part:
            continue
        term = part
        weight = 10
        if "|" in part:
            maybe_term, maybe_weight = part.rsplit("|", 1)
            term = maybe_term.strip()
            try:
                weight = int(maybe_weight.strip())
            except ValueError:
                weight = 10
        cleaned_term = _clean_term(term)
        if cleaned_term:
            entries.append(
                _HotwordEntry(
                    term=cleaned_term,
                    weight=_normalize_tencent_hotword_weight(weight),
                    priority=-1,
                )
            )
    return entries


def _build_tencent_hotword_list_from_entries(entries: list[_HotwordEntry]) -> str | None:
    hotwords: list[str] = []
    seen: set[str] = set()

    for entry in sorted(entries, key=lambda item: (item.priority, -item.weight, item.term)):
        term = _clean_term(entry.term)
        if not term or len(term) > _TENCENT_MAX_HOTWORD_CHARS:
            continue
        if "," in term or "|" in term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        if entry.weight == 100 or 1 <= entry.weight <= 11:
            resolved_weight = entry.weight
        else:
            resolved_weight = _normalize_tencent_hotword_weight(entry.weight)
        hotwords.append(f"{term}|{resolved_weight}")
        if len(hotwords) >= _TENCENT_MAX_HOTWORDS:
            break

    return ",".join(hotwords) if hotwords else None


async def build_tencent_hotword_list() -> str | None:
    settings = get_settings()
    manual_entries = _parse_manual_hotword_entries(settings.tencent_asr_hotword_list)
    if not settings.tencent_asr_dynamic_hotwords_enabled:
        return _build_tencent_hotword_list_from_entries(manual_entries)

    entries: list[_HotwordEntry] = []
    entries.extend(manual_entries)

    try:
        async with _session_factory() as db:
            result = await db.execute(
                select(HotwordGroup)
                .where(HotwordGroup.is_active.is_(True))
                .options(selectinload(HotwordGroup.words))
            )
            groups = list(result.scalars().all())
    except Exception as exc:
        logger.warning("Failed to load hotword groups for Tencent ASR, using fallback hotwords only: %s", exc)
        entries.extend(_load_default_hotword_entries())
        return _build_tencent_hotword_list_from_entries(entries)

    for group in sorted(groups, key=lambda item: (_priority_for_group(item.group_type), item.name)):
        if not _is_tencent_hotword_group_enabled(group):
            continue
        priority = _priority_for_group(group.group_type)
        active_words = sorted(
            (word for word in group.words if word.is_active),
            key=lambda item: (-item.weight, item.word),
        )
        for word in active_words:
            term = _clean_term(word.word)
            if not term:
                continue
            entries.append(
                _HotwordEntry(
                    term=term,
                    weight=_normalize_tencent_hotword_weight(
                        _effective_db_hotword_weight(word.weight, group.group_type)
                    ),
                    priority=priority,
                )
            )

    return _build_tencent_hotword_list_from_entries(entries)


def normalize_medical_aesthetic_text(text: str) -> tuple[str, list[dict[str, str]]]:
    normalized = str(text or "")
    corrections: list[dict[str, str]] = []

    for source, target in _TEXT_REPLACEMENTS:
        if source not in normalized:
            continue
        normalized = normalized.replace(source, target)
        corrections.append({"from": source, "to": target, "type": "literal"})

    for pattern, replacement in _PHRASE_REPLACEMENTS:
        matches = list(pattern.finditer(normalized))
        if not matches:
            continue
        normalized = pattern.sub(replacement, normalized)
        for match in matches:
            target = match.expand(replacement)
            if match.group(0) == target:
                continue
            corrections.append({"from": match.group(0), "to": target, "type": "pattern"})

    return normalized, corrections


def apply_medical_aesthetic_term_normalization(utterances: list[dict]) -> tuple[list[dict], int]:
    if not get_settings().asr_medical_term_normalization_enabled:
        return utterances, 0

    normalized_utterances: list[dict] = []
    total_corrections = 0

    for utterance in utterances:
        clone = dict(utterance)
        original_text = str(clone.get("text") or "")
        if not original_text.strip():
            normalized_utterances.append(clone)
            continue

        normalized_text, corrections = normalize_medical_aesthetic_text(original_text)
        if corrections:
            clone["text"] = normalized_text
            clone["text_original"] = original_text
            clone["term_corrections"] = corrections
            total_corrections += len(corrections)
        normalized_utterances.append(clone)

    return normalized_utterances, total_corrections
