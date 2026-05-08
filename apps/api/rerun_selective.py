"""Re-run analysis on Apr 19-20 recordings using updated prompts."""
from __future__ import annotations
import asyncio, json, sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'src'))

from smart_badge_api.analysis.pipeline import analyze_transcript
from smart_badge_api.analysis.prompt_builder import build_system_prompt
from smart_badge_api.recording_analysis_service import build_analysis_payload_from_utterances
from smart_badge_api.db.session import _session_factory as _async_session

BASE = Path("/opt/badge/apps/api/uploads/dingtalk_staging")
MANIFESTS_DIR = BASE / "manifests"
TRANSCRIPTS_DIR = BASE / "transcripts"
RESULTS_DIR = BASE / "results"
ANALYSIS_INPUT_DIR = BASE / "analysis_input"


def normalize_path(p) -> Path | None:
    if not p: return None
    s = str(p)
    if s.startswith('/app/'):
        return Path('uploads') / Path(s[len('/app/uploads/'):])
    return Path(s)


async def get_system_prompt() -> str:
    async with _async_session() as db:
        return await build_system_prompt(db)


def main():
    system_prompt = asyncio.run(get_system_prompt())
    print(f"[INFO] System prompt loaded ({len(system_prompt)} chars)")

    manifests = sorted(MANIFESTS_DIR.glob("*.json"))
    targets = []
    for mf in manifests:
        m = json.loads(mf.read_text())
        if m.get("status") == "filtered":
            continue
        rca = m.get("remoteCreatedAt", "")
        if rca.startswith("2026-04-19") or rca.startswith("2026-04-20"):
            targets.append((mf, m))

    print(f"[INFO] Found {len(targets)} target recordings for Apr 19-20")

    success = fail = skip = 0
    for i, (mf, m) in enumerate(targets, 1):
        sk = m["stageKey"]

        # Find transcript
        tpath = normalize_path(m.get("transcriptPath"))
        if tpath is None or not tpath.exists():
            tpath = TRANSCRIPTS_DIR / f"{sk}.transcript.json"
        if not tpath.exists():
            print(f"[{i}/{len(targets)}] SKIP {sk} - no transcript")
            skip += 1
            continue

        transcript = json.loads(tpath.read_text())
        utterances = transcript.get("utterances") if isinstance(transcript, dict) else transcript
        if not utterances:
            print(f"[{i}/{len(targets)}] SKIP {sk} - empty transcript")
            skip += 1
            continue

        # Build analysis input
        try:
            payload, seg_count, _dur = build_analysis_payload_from_utterances(utterances)
            if seg_count == 0:
                print(f"[{i}/{len(targets)}] SKIP {sk} - 0 segments")
                skip += 1
                continue
        except Exception as e:
            print(f"[{i}/{len(targets)}] SKIP {sk} - payload error: {e}")
            skip += 1
            continue

        ANALYSIS_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        input_path = ANALYSIS_INPUT_DIR / f"{sk}.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False))

        print(f"[{i}/{len(targets)}] Analyzing {sk} ({seg_count} segs)...", flush=True)
        t0 = time.time()

        try:
            result = analyze_transcript(str(input_path), system_prompt=system_prompt)
            result_dict = result.model_dump()

            result_path = RESULTS_DIR / f"{sk}.result.json"
            result_path.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2))

            m["analysisResultPath"] = str(result_path)
            mf.write_text(json.dumps(m, ensure_ascii=False, indent=2))

            elapsed = time.time() - t0
            tags = len(result_dict.get("customer_profile", {}).get("tags", []))
            concerns = len(result_dict.get("customer_concerns", {}).get("items", []))
            recs = len(result_dict.get("staff_recommendations", {}).get("items", []))
            indications = len(result_dict.get("standardized_indications", {}).get("items", []))
            print(f"  OK ({elapsed:.0f}s) tags={tags} concerns={concerns} recs={recs} indications={indications}")
            success += 1

        except Exception as e:
            elapsed = time.time() - t0
            print(f"  FAIL ({elapsed:.0f}s): {e}")
            traceback.print_exc()
            fail += 1

    print(f"\n[DONE] success={success} fail={fail} skip={skip}")


if __name__ == "__main__":
    main()
