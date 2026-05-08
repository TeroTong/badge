from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from smart_badge_api.asr.domain_terms import (
    _TEXT_REPLACEMENTS,
    _load_default_hotword_entries,
    apply_medical_aesthetic_term_normalization,
    build_tencent_hotword_list,
)
from smart_badge_api.asr.tencent_cloud_provider import transcribe_audio as tencent_transcribe_audio
from smart_badge_api.visit_order_matching import _PROCEDURE_TERM_VARIANTS, _PROJECT_BUCKET_KEYWORDS

_TEXT_SPLIT_RE = re.compile(r"[，。！？、；：,.!?\s/()（）【】\\[\\]“”\"'<>《》]+")
_TEXT_KEEP_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
_SUSPICIOUS_STOPWORDS = {
    "我们", "你们", "这个", "那个", "就是", "现在", "一下", "感觉", "可以", "老师",
    "医生", "顾问", "客户", "咨询", "今天", "之前", "然后", "因为", "不是", "自己",
}


@dataclass(slots=True)
class SampleInput:
    audio_path: str
    label: str
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None


@dataclass(slots=True)
class SampleSummary:
    label: str
    audio_path: str
    utterance_count: int
    duration_ms: int
    correction_count: int
    correction_examples: list[dict[str, Any]]
    error: str | None = None


DEFAULT_SAMPLES = [
    SampleInput(
        audio_path="/app/uploads/dingtalk_staging/archive/SSYX41022500/202603/18_110646.mp3",
        label="18_110646",
        staff_id="3fb42f0d68c4",
        staff_name="兰四秀",
        staff_role="consultant",
    ),
    SampleInput(
        audio_path="/app/uploads/dingtalk_staging/archive/SSYX41022500/202603/17_123948.mp3",
        label="17_123948",
        staff_id="3fb42f0d68c4",
        staff_name="兰四秀",
        staff_role="consultant",
    ),
    SampleInput(
        audio_path="/app/uploads/dingtalk_staging/archive/SSYX41022508/202603/24_170636.mp3",
        label="24_170636",
    ),
    SampleInput(
        audio_path="/app/uploads/dingtalk_staging/archive/SSYX41022508/202603/17_143509.mp3",
        label="17_143509",
    ),
]
_MAX_DIRECT_BYTES = 5_000_000


def _load_domain_lexicon() -> set[str]:
    terms: set[str] = set()
    for entry in _load_default_hotword_entries():
        if entry.term:
            terms.add(entry.term)
    for source, target in _TEXT_REPLACEMENTS:
        if source:
            terms.add(source)
        if target:
            terms.add(target)
    for keywords in _PROJECT_BUCKET_KEYWORDS.values():
        terms.update(keyword for keyword in keywords if keyword)
    for variants in _PROCEDURE_TERM_VARIANTS.values():
        terms.update(term for term in variants if term)
    return {term for term in terms if len(term) >= 2}


def _iter_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    for part in _TEXT_SPLIT_RE.split(text):
        for token in _TEXT_KEEP_RE.findall(part):
            cleaned = token.strip()
            if cleaned:
                chunks.append(cleaned)
    return chunks


def _iter_candidate_ngrams(text: str, *, min_len: int = 3, max_len: int = 6) -> list[str]:
    chunks = _iter_chunks(text)
    results: list[str] = []
    for chunk in chunks:
        if len(chunk) < min_len:
            continue
        if chunk.isdigit():
            continue
        if min_len <= len(chunk) <= max_len:
            results.append(chunk)
        upper = min(max_len, len(chunk))
        for size in range(min_len, upper + 1):
            for start in range(0, len(chunk) - size + 1):
                piece = chunk[start:start + size]
                if piece.isdigit():
                    continue
                results.append(piece)
    return results


def _best_domain_match(term: str, lexicon: set[str]) -> tuple[str, float] | None:
    if len(term) < 3 or term in lexicon or term in _SUSPICIOUS_STOPWORDS:
        return None

    best_term = ""
    best_score = 0.0
    for candidate in lexicon:
        if len(candidate) < 3:
            continue
        if abs(len(candidate) - len(term)) > 1:
            continue
        if term == candidate:
            continue
        score = SequenceMatcher(None, term, candidate).ratio()
        if score > best_score:
            best_term = candidate
            best_score = score

    if best_score < 0.74:
        return None
    return best_term, best_score


async def _transcribe_sample(sample: SampleInput) -> tuple[SampleSummary, list[dict[str, Any]], list[dict[str, Any]]]:
    audio_size = Path(sample.audio_path).stat().st_size
    if audio_size > _MAX_DIRECT_BYTES:
        summary = SampleSummary(
            label=sample.label,
            audio_path=sample.audio_path,
            utterance_count=0,
            duration_ms=0,
            correction_count=0,
            correction_examples=[],
            error=f"skipped_oversize:{audio_size}",
        )
        return summary, [], []

    hotword_list = await build_tencent_hotword_list()
    try:
        raw_utterances, _, duration_ms = await tencent_transcribe_audio(
            sample.audio_path,
            hotword_list=hotword_list,
        )
    except Exception as exc:
        summary = SampleSummary(
            label=sample.label,
            audio_path=sample.audio_path,
            utterance_count=0,
            duration_ms=0,
            correction_count=0,
            correction_examples=[],
            error=str(exc),
        )
        return summary, [], []

    normalized_utterances, correction_count = apply_medical_aesthetic_term_normalization(raw_utterances)
    correction_examples = [
        {
            "original": item.get("text_original"),
            "normalized": item.get("text"),
            "corrections": item.get("term_corrections") or [],
        }
        for item in normalized_utterances
        if item.get("term_corrections")
    ][:10]
    summary = SampleSummary(
        label=sample.label,
        audio_path=sample.audio_path,
        utterance_count=len(normalized_utterances),
        duration_ms=duration_ms,
        correction_count=correction_count,
        correction_examples=correction_examples,
    )
    return summary, raw_utterances, normalized_utterances


def _mine_candidates(raw_utterances_by_label: dict[str, list[dict[str, Any]]], lexicon: set[str]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)

    for label, utterances in raw_utterances_by_label.items():
        for utterance in utterances:
            text = str(utterance.get("text") or "")
            for gram in _iter_candidate_ngrams(text):
                match = _best_domain_match(gram, lexicon)
                if match is None:
                    continue
                suggested, score = match
                key = (gram, suggested)
                counts[key] += 1
                if len(examples[key]) < 3:
                    examples[key].append(
                        {
                            "label": label,
                            "text": text,
                            "score": f"{score:.2f}",
                        }
                    )

    result: list[dict[str, Any]] = []
    for (raw_term, suggested), count in counts.most_common():
        if count < 2:
            continue
        result.append(
            {
                "raw_term": raw_term,
                "suggested": suggested,
                "count": count,
                "examples": examples[(raw_term, suggested)],
            }
        )
    return result[:30]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run sample ASR transcriptions and mine likely medical-term mistakes.")
    parser.add_argument("--output", type=str, default="", help="Optional JSON report output path")
    args = parser.parse_args()

    lexicon = _load_domain_lexicon()
    raw_utterances_by_label: dict[str, list[dict[str, Any]]] = {}
    summaries: list[SampleSummary] = []

    for sample in DEFAULT_SAMPLES:
        summary, raw_utterances, _ = await _transcribe_sample(sample)
        summaries.append(summary)
        raw_utterances_by_label[sample.label] = raw_utterances

    direct_corrections: Counter[tuple[str, str]] = Counter()
    for summary in summaries:
        for example in summary.correction_examples:
            for item in example.get("corrections") or []:
                direct_corrections[(item.get("from") or "", item.get("to") or "")] += 1

    direct_items = [
        {"raw_term": raw_term, "suggested": suggested, "count": count}
        for (raw_term, suggested), count in direct_corrections.most_common()
    ]
    fuzzy_candidates = _mine_candidates(raw_utterances_by_label, lexicon)

    report = {
        "samples": [asdict(item) for item in summaries],
        "direct_corrections": direct_items,
        "fuzzy_candidates": fuzzy_candidates,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
