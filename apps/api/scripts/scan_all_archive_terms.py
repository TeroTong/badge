from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

os.environ.setdefault("TENCENT_ASR_LOCAL_DIARIZATION_ENABLED", "false")

try:
    from mutagen import File as mutagen_file
except Exception:
    mutagen_file = None

from smart_badge_api.asr.domain_terms import (
    _TEXT_REPLACEMENTS,
    _load_default_hotword_entries,
    apply_medical_aesthetic_term_normalization,
    build_tencent_hotword_list,
)
from smart_badge_api.asr.hotword_mining import (
    build_auto_hotword_candidates,
    upsert_auto_hotword_candidates,
)
from smart_badge_api.asr.tencent_cloud_provider import transcribe_audio as tencent_transcribe_audio
from smart_badge_api.core.config import get_settings
from smart_badge_api.db.session import _session_factory
from smart_badge_api.dingtalk_audio_sync import (
    CUSTOMER_CONSULTATION_KEYWORDS,
    INTERNAL_DISCUSSION_KEYWORDS,
    _keyword_hit_count,
    _pre_asr_quality_decision,
)
from smart_badge_api.visit_order_matching import _PROCEDURE_TERM_VARIANTS, _PROJECT_BUCKET_KEYWORDS

_TEXT_SPLIT_RE = re.compile(r"[，。！？、；：,.!?\s/()（）【】\\[\\]“”\"'<>《》]+")
_TEXT_KEEP_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
_GRAMMATICAL_PREFIXES = ("的", "对", "个", "这", "那", "把", "跟", "给")
_GRAMMATICAL_SUFFIXES = ("的", "哈", "啊", "呢", "吗", "嘛", "呀", "哦", "了")
_MAX_DIRECT_BYTES = 5_000_000
TEST_OR_ACCIDENTAL_KEYWORDS = {
    "测试",
    "试音",
    "试一下",
    "喂喂",
    "喂",
    "听得到",
    "听得见",
    "一二三",
    "123",
    "点错",
    "误触",
    "误操作",
    "忘关",
    "没关",
    "先挂了",
    "先挂",
    "先这样",
}


@dataclass(slots=True)
class RecordingTarget:
    audio_path: str
    label: str
    file_size_bytes: int
    duration_seconds: float | None
    manifest: dict[str, Any] | None = None
    transcript_path: str | None = None
    manifest_status: str | None = None
    staff_id: str | None = None
    staff_name: str | None = None
    staff_role: str | None = None


@dataclass(slots=True)
class ScanItem:
    label: str
    audio_path: str
    source: str
    file_size_bytes: int
    duration_seconds: float | None
    status: str
    reason: str | None = None
    utterance_count: int = 0
    transcript_chars: int = 0
    correction_count: int = 0
    consultation_keyword_hits: int = 0
    internal_keyword_hits: int = 0
    test_keyword_hits: int = 0
    known_term_hits: int = 0
    transcript_provider: str | None = None


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


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


def _relevant_domain_terms(lexicon: set[str]) -> set[str]:
    ignored = {"顾客", "客户", "咨询", "方案", "预算", "价格", "面诊", "医生", "院长", "设计师"}
    return {term for term in lexicon if len(term) >= 2 and term not in ignored}


def _iter_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    for part in _TEXT_SPLIT_RE.split(text):
        for token in _TEXT_KEEP_RE.findall(part):
            cleaned = token.strip()
            if cleaned:
                chunks.append(cleaned)
    return chunks


def _iter_candidate_ngrams(text: str, *, min_len: int = 3, max_len: int = 8) -> list[str]:
    chunks = _iter_chunks(text)
    results: list[str] = []
    for chunk in chunks:
        if len(chunk) < min_len or chunk.isdigit():
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


def _is_noise_candidate(term: str, suggested: str) -> bool:
    if len(term) < 3:
        return True
    if term.isdigit():
        return True
    if any(term.startswith(prefix) for prefix in _GRAMMATICAL_PREFIXES):
        return True
    if any(term.endswith(suffix) for suffix in _GRAMMATICAL_SUFFIXES):
        return True
    if term in {"这个班", "动格", "学设计师", "设计师哈"}:
        return False
    if term == suggested:
        return True
    if term.endswith(suggested) or term.startswith(suggested):
        return True
    return False


def _best_domain_match(term: str, lexicon: set[str]) -> tuple[str, float] | None:
    best_term = ""
    best_score = 0.0
    for candidate in lexicon:
        if len(candidate) < 3:
            continue
        if abs(len(candidate) - len(term)) > 2:
            continue
        score = SequenceMatcher(None, term, candidate).ratio()
        if score > best_score:
            best_term = candidate
            best_score = score
    if best_score < 0.74:
        return None
    return best_term, best_score


def _probe_duration_seconds(audio_path: Path) -> float | None:
    if mutagen_file is None:
        return None
    try:
        audio = mutagen_file(str(audio_path))
    except Exception:
        return None
    if audio is None or getattr(audio, "info", None) is None:
        return None
    duration = getattr(audio.info, "length", None)
    if duration is None:
        return None
    return float(duration or 0.0) or None


def _load_manifest_index(stage_root: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in sorted((stage_root / "manifests").glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        audio_path = str(manifest.get("audioPath") or "").strip()
        if audio_path:
            index[audio_path] = manifest
    return index


def _discover_targets(stage_root: Path) -> list[RecordingTarget]:
    manifest_index = _load_manifest_index(stage_root)
    audio_paths: dict[str, RecordingTarget] = {}

    for audio_path, manifest in manifest_index.items():
        path = Path(audio_path)
        if not path.exists():
            continue
        audio_paths[str(path)] = RecordingTarget(
            audio_path=str(path),
            label=manifest.get("stageKey") or path.stem,
            file_size_bytes=path.stat().st_size,
            duration_seconds=float(manifest.get("durationSeconds") or 0) or _probe_duration_seconds(path),
            manifest=manifest,
            transcript_path=manifest.get("transcriptPath"),
            manifest_status=manifest.get("status"),
            staff_id=manifest.get("staffId"),
            staff_name=manifest.get("staffName"),
            staff_role=manifest.get("staffRole"),
        )

    for path in sorted((stage_root / "archive").rglob("*.mp3")):
        resolved = str(path)
        if resolved in audio_paths:
            continue
        audio_paths[resolved] = RecordingTarget(
            audio_path=resolved,
            label=f"{path.parent.parent.name}_{path.stem}",
            file_size_bytes=path.stat().st_size,
            duration_seconds=_probe_duration_seconds(path),
        )

    return sorted(audio_paths.values(), key=lambda item: (item.audio_path,))


def _load_transcript_document(path: str) -> dict[str, Any] | None:
    transcript_path = Path(path)
    if not transcript_path.is_file():
        return None
    return json.loads(transcript_path.read_text(encoding="utf-8"))


def _transcript_provider(document: dict[str, Any]) -> str | None:
    return (
        str(document.get("asrProvider") or "").strip()
        or str(document.get("transcript_provider") or "").strip()
        or str(document.get("provider") or "").strip()
        or None
    )


def _full_text_from_utterances(utterances: list[dict]) -> str:
    return " ".join(str(item.get("text") or "") for item in utterances if str(item.get("text") or "").strip()).strip()


def _known_term_counter(utterances: list[dict], domain_terms: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in utterances:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        for term in domain_terms:
            hits = text.count(term)
            if hits > 0:
                counts[term] += hits
    return counts


def _term_mining_quality(
    utterances: list[dict],
    full_text: str,
    domain_terms: set[str],
) -> tuple[bool, str | None, int, int, int, int]:
    settings = get_settings()
    valid_utterances = [item for item in utterances if str(item.get("text") or "").strip()]
    utterance_count = len(valid_utterances)
    text_length = len(full_text.strip())
    if settings.dingtalk_audio_min_utterance_count > 0 and utterance_count < settings.dingtalk_audio_min_utterance_count:
        return False, "utterance_too_few", 0, 0, 0, 0
    if settings.dingtalk_audio_min_transcript_chars > 0 and text_length < settings.dingtalk_audio_min_transcript_chars:
        return False, "transcript_too_short", 0, 0, 0, 0

    consultation_hits = _keyword_hit_count(full_text, CUSTOMER_CONSULTATION_KEYWORDS)
    internal_hits = _keyword_hit_count(full_text, INTERNAL_DISCUSSION_KEYWORDS)
    test_hits = _keyword_hit_count(full_text, TEST_OR_ACCIDENTAL_KEYWORDS)
    known_hits = sum(1 for term in domain_terms if term in full_text)

    if internal_hits >= max(settings.dingtalk_audio_internal_keyword_threshold, 1):
        if consultation_hits == 0 or internal_hits >= consultation_hits + 2:
            return False, "internal_discussion", consultation_hits, internal_hits, test_hits, known_hits

    if (
        test_hits > 0
        and consultation_hits == 0
        and known_hits < 2
        and (utterance_count <= 8 or text_length < 120)
    ):
        return False, "test_or_accidental", consultation_hits, internal_hits, test_hits, known_hits

    if consultation_hits == 0 and known_hits < 2:
        return False, "non_consultation_like", consultation_hits, internal_hits, test_hits, known_hits

    return True, None, consultation_hits, internal_hits, test_hits, known_hits


async def _process_existing_transcript(
    target: RecordingTarget,
    transcript_document: dict[str, Any],
    domain_terms: set[str],
) -> tuple[ScanItem, list[dict[str, Any]], Counter[str]]:
    pre_decision = _pre_asr_quality_decision(int(round(target.duration_seconds or 0)) if target.duration_seconds else None)
    if not pre_decision.passed:
        return (
            ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="existing_transcript",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="skipped",
                reason=pre_decision.reason,
                transcript_provider=_transcript_provider(transcript_document),
            ),
            [],
            Counter(),
        )

    provider = _transcript_provider(transcript_document)
    if provider == "mock":
        return (
            ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="existing_transcript",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="skipped",
                reason="mock_transcript",
                transcript_provider=provider,
            ),
            [],
            Counter(),
        )

    raw_utterances = list(transcript_document.get("utterances") or [])
    normalized_utterances, correction_count = apply_medical_aesthetic_term_normalization(raw_utterances)
    full_text = _full_text_from_utterances(normalized_utterances) or str(transcript_document.get("fullText") or "").strip()
    passed, reason, consultation_hits, internal_hits, test_hits, known_hits = _term_mining_quality(
        normalized_utterances,
        full_text,
        domain_terms,
    )
    item = ScanItem(
        label=target.label,
        audio_path=target.audio_path,
        source="existing_transcript",
        file_size_bytes=target.file_size_bytes,
        duration_seconds=target.duration_seconds,
        status="included" if passed else "skipped",
        reason=reason,
        utterance_count=len(normalized_utterances),
        transcript_chars=len(full_text),
        correction_count=correction_count,
        consultation_keyword_hits=consultation_hits,
        internal_keyword_hits=internal_hits,
        test_keyword_hits=test_hits,
        known_term_hits=known_hits,
        transcript_provider=provider,
    )
    return item, raw_utterances if passed else [], _known_term_counter(normalized_utterances, domain_terms) if passed else Counter()


async def _process_new_transcription(
    target: RecordingTarget,
    *,
    hotword_list: str | None,
    domain_terms: set[str],
) -> tuple[ScanItem, list[dict[str, Any]], Counter[str]]:
    pre_decision = _pre_asr_quality_decision(int(round(target.duration_seconds or 0)) if target.duration_seconds else None)
    if not pre_decision.passed:
        return (
            ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="transcribed",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="skipped",
                reason=pre_decision.reason,
            ),
            [],
            Counter(),
        )
    if target.file_size_bytes > _MAX_DIRECT_BYTES:
        return (
            ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="transcribed",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="skipped",
                reason=f"oversize_for_tencent:{target.file_size_bytes}",
            ),
            [],
            Counter(),
        )

    try:
        raw_utterances, _, duration_ms = await tencent_transcribe_audio(
            target.audio_path,
            hotword_list=hotword_list,
            source_id=f"archive_term_scan::{target.audio_path}",
        )
    except Exception as exc:
        return (
            ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="transcribed",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="failed",
                reason=str(exc),
            ),
            [],
            Counter(),
        )

    normalized_utterances, correction_count = apply_medical_aesthetic_term_normalization(raw_utterances)
    full_text = _full_text_from_utterances(normalized_utterances)
    passed, reason, consultation_hits, internal_hits, test_hits, known_hits = _term_mining_quality(
        normalized_utterances,
        full_text,
        domain_terms,
    )
    item = ScanItem(
        label=target.label,
        audio_path=target.audio_path,
        source="transcribed",
        file_size_bytes=target.file_size_bytes,
        duration_seconds=target.duration_seconds or (duration_ms / 1000 if duration_ms else None),
        status="included" if passed else "skipped",
        reason=reason,
        utterance_count=len(normalized_utterances),
        transcript_chars=len(full_text),
        correction_count=correction_count,
        consultation_keyword_hits=consultation_hits,
        internal_keyword_hits=internal_hits,
        test_keyword_hits=test_hits,
        known_term_hits=known_hits,
        transcript_provider="tencent_asr",
    )
    return item, raw_utterances if passed else [], _known_term_counter(normalized_utterances, domain_terms) if passed else Counter()


def _mine_candidates(raw_utterances_by_label: dict[str, list[dict[str, Any]]], lexicon: set[str]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)

    for label, utterances in raw_utterances_by_label.items():
        for utterance in utterances:
            text = str(utterance.get("text") or "")
            if not text:
                continue
            for gram in _iter_candidate_ngrams(text):
                match = _best_domain_match(gram, lexicon)
                if match is None:
                    continue
                suggested, score = match
                if _is_noise_candidate(gram, suggested):
                    continue
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
    return result[:80]


def _build_report(
    *,
    items: list[ScanItem],
    known_term_counts: Counter[str],
    raw_utterances_by_label: dict[str, list[dict[str, Any]]] | None,
    lexicon: set[str],
) -> dict[str, Any]:
    summary = Counter(item.status for item in items)
    by_reason = Counter(item.reason or "ok" for item in items)
    report: dict[str, Any] = {
        "summary": {
            "total": len(items),
            "included": summary.get("included", 0),
            "skipped": summary.get("skipped", 0),
            "failed": summary.get("failed", 0),
        },
        "by_reason": dict(by_reason.most_common()),
        "items": [asdict(item) for item in items],
        "known_term_hits": [
            {"term": term, "count": count}
            for term, count in known_term_counts.most_common(200)
        ],
    }
    if raw_utterances_by_label is not None:
        direct_corrections: Counter[tuple[str, str]] = Counter()
        for utterances in raw_utterances_by_label.values():
            _, correction_count = apply_medical_aesthetic_term_normalization(utterances)
            if correction_count <= 0:
                continue
            normalized_utterances, _ = apply_medical_aesthetic_term_normalization(utterances)
            for item in normalized_utterances:
                for correction in item.get("term_corrections") or []:
                    direct_corrections[(correction.get("from") or "", correction.get("to") or "")] += 1
        report["direct_corrections"] = [
            {"raw_term": raw_term, "suggested": suggested, "count": count}
            for (raw_term, suggested), count in direct_corrections.most_common()
        ]
        report["fuzzy_candidates"] = _mine_candidates(raw_utterances_by_label, lexicon)

    return report


def _write_report(output_path: Path, report: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scan all archived recordings and mine ASR medical-term candidates.")
    parser.add_argument(
        "--stage-root",
        default="/app/uploads/dingtalk_staging",
        help="Stage root containing archive/manifests/transcripts",
    )
    parser.add_argument(
        "--output",
        default="/app/uploads/asr_runtime/full_archive_term_scan.json",
        help="JSON report output path",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional limit for discovered recordings")
    parser.add_argument(
        "--sync-hotwords",
        action="store_true",
        help="Upsert mined high-confidence hotwords into the managed hotword group",
    )
    args = parser.parse_args()

    get_settings.cache_clear()
    stage_root = Path(args.stage_root)
    output_path = Path(args.output)
    lexicon = _load_domain_lexicon()
    domain_terms = _relevant_domain_terms(lexicon)
    hotword_list = await build_tencent_hotword_list()

    targets = _discover_targets(stage_root)
    if args.limit > 0:
        targets = targets[:args.limit]

    items: list[ScanItem] = []
    known_term_counts: Counter[str] = Counter()
    raw_utterances_by_label: dict[str, list[dict[str, Any]]] = {}

    for index, target in enumerate(targets, start=1):
        transcript_document = None
        if target.manifest_status == "filtered":
            item = ScanItem(
                label=target.label,
                audio_path=target.audio_path,
                source="existing_transcript" if target.transcript_path else "transcribed",
                file_size_bytes=target.file_size_bytes,
                duration_seconds=target.duration_seconds,
                status="skipped",
                reason="existing_filtered",
            )
            items.append(item)
        else:
            if target.transcript_path:
                transcript_document = _load_transcript_document(target.transcript_path)
            if transcript_document:
                item, raw_utterances, term_counts = await _process_existing_transcript(
                    target,
                    transcript_document,
                    domain_terms,
                )
            else:
                item, raw_utterances, term_counts = await _process_new_transcription(
                    target,
                    hotword_list=hotword_list,
                    domain_terms=domain_terms,
                )
            items.append(item)
            if item.status == "included":
                raw_utterances_by_label[target.label] = raw_utterances
                known_term_counts.update(term_counts)

        if index % 5 == 0 or index == len(targets):
            _write_report(
                output_path,
                _build_report(
                    items=items,
                    known_term_counts=known_term_counts,
                    raw_utterances_by_label=None,
                    lexicon=lexicon,
                ),
            )
            print(
                f"[{index}/{len(targets)}] included={sum(1 for x in items if x.status == 'included')} "
                f"skipped={sum(1 for x in items if x.status == 'skipped')} "
                f"failed={sum(1 for x in items if x.status == 'failed')}",
                flush=True,
            )

    report = _build_report(
        items=items,
        known_term_counts=known_term_counts,
        raw_utterances_by_label=raw_utterances_by_label,
        lexicon=lexicon,
    )
    if args.sync_hotwords:
        candidates = build_auto_hotword_candidates(report)
        async with _session_factory() as db:
            report["auto_hotword_sync"] = await upsert_auto_hotword_candidates(db, candidates)

    _write_report(output_path, report)
    print(f"Report written to {output_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
