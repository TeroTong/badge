"""
批量为已有 transcript 但分析结果缺失或仍是旧 schema 的录音重跑分析。
不会调用 ASR，只读取已有 transcript -> 构建 analysis input -> 调用 LLM -> 写入 result.
"""
from __future__ import annotations
import asyncio, json, sys, time
from pathlib import Path
from typing import Any

# Allow imports
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from smart_badge_api.analysis.pipeline import analyze_transcript
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.recording_analysis_service import build_analysis_payload_from_utterances
from smart_badge_api.db.session import _session_factory as _async_session


def normalize_path(p) -> Path | None:
    if not p: return None
    s = str(p)
    if s.startswith('/app/'):
        return Path('uploads') / Path(s[len('/app/uploads/'):])
    return Path(s)


def needs_rerun(manifest: dict[str, Any], base: Path) -> tuple[bool, Path | None, Path | None]:
    """Return (needs_rerun, transcript_path, existing_result_path)."""
    tpath = normalize_path(manifest.get('transcriptPath'))
    if tpath is None or not tpath.exists():
        # try base-relative
        if tpath is not None:
            alt = base.parent / tpath
            if alt.exists():
                tpath = alt
            else:
                return False, None, None
        else:
            return False, None, None
    apath_raw = manifest.get('analysisResultPath')
    apath = normalize_path(apath_raw)
    if apath is not None and not apath.exists():
        alt = base.parent / apath
        if alt.exists():
            apath = alt
    if apath is None or not apath.exists():
        return True, tpath, None
    try:
        a = json.load(open(apath))
        if isinstance(a, dict) and isinstance(a.get('consultation_result'), dict):
            return False, tpath, apath
    except Exception:
        pass
    return True, tpath, apath


async def get_system_prompt() -> str:
    async with _async_session() as db:
        return await build_system_prompt(db)


def rerun_one(stage_key: str, manifest_path: Path, transcript_path: Path, base: Path, system_prompt: str) -> dict[str, Any]:
    """Run analysis for one recording. Returns result info dict."""
    # 1. Build analysis input payload
    transcript = json.load(open(transcript_path))
    utterances = transcript.get('utterances') or []
    payload, segment_count, _duration_ms = build_analysis_payload_from_utterances(utterances)
    if segment_count == 0:
        raise ValueError(f'no valid utterances in {transcript_path.name}')

    analysis_input_dir = base / 'analysis_input'
    analysis_input_dir.mkdir(parents=True, exist_ok=True)
    analysis_input_path = analysis_input_dir / f'{stage_key}.json'
    analysis_input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    # 2. Call LLM analysis
    result = analyze_transcript(str(analysis_input_path), system_prompt=system_prompt)
    result_dict = result.model_dump()

    # 3. Write result file
    result_dir = base / 'results'
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f'{stage_key}.result.json'
    result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding='utf-8')

    # 4. Update manifest
    manifest = json.load(open(manifest_path))
    manifest['analysisInputPath'] = str(analysis_input_path)
    manifest['analysisResultPath'] = str(result_path)
    manifest['status'] = 'analyzed'
    manifest.pop('errorMessage', None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

    return {
        'stage_key': stage_key,
        'segments': segment_count,
        'has_consultation_result': isinstance(result_dict.get('consultation_result'), dict),
        'result_path': str(result_path),
    }


async def main(limit: int | None = None, dry_run: bool = False):
    base = Path('uploads/dingtalk_staging')
    manifests_dir = base / 'manifests'

    candidates: list[tuple[str, Path, Path]] = []
    for mp in sorted(manifests_dir.glob('*.json')):
        try:
            m = json.load(open(mp))
        except Exception:
            continue
        if m.get('status') == 'filtered':
            continue
        ok, tpath, _ = needs_rerun(m, base)
        if ok and tpath is not None:
            stage_key = mp.stem  # filename without .json
            candidates.append((stage_key, mp, tpath))

    print(f'Found {len(candidates)} recordings to rerun analysis.', flush=True)
    if dry_run:
        for c in candidates[:10]:
            print(' ', c[0])
        return

    if limit is not None:
        candidates = candidates[:limit]

    print('Building system prompt...', flush=True)
    system_prompt = await get_system_prompt()
    print(f'System prompt length: {len(system_prompt)} chars', flush=True)

    success = []
    failures = []
    t_start = time.time()
    for idx, (stage_key, mp, tpath) in enumerate(candidates, 1):
        t0 = time.time()
        try:
            info = rerun_one(stage_key, mp, tpath, base, system_prompt)
            elapsed = time.time() - t0
            success.append(info)
            print(f'[{idx}/{len(candidates)}] OK {stage_key} | segments={info["segments"]} | new_schema={info["has_consultation_result"]} | {elapsed:.1f}s', flush=True)
        except Exception as exc:
            failures.append((stage_key, str(exc)))
            print(f'[{idx}/{len(candidates)}] FAIL {stage_key}: {exc}', flush=True)

    total_elapsed = time.time() - t_start
    print(f'\nDone in {total_elapsed:.1f}s. Success: {len(success)} | Failed: {len(failures)}', flush=True)
    if failures:
        print('\nFailures:')
        for s, e in failures:
            print(f'  {s}: {e}')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=None, help='Process at most N recordings')
    p.add_argument('--dry-run', action='store_true', help='Only list candidates, do not run')
    args = p.parse_args()
    asyncio.run(main(limit=args.limit, dry_run=args.dry_run))
