"""Batch fix: re-run rebuild_consultation_process_evaluation for all results
to clear contradictory issues on passed checkpoints."""
import json
from pathlib import Path

from smart_badge_api.analysis.consultation_evaluation import rebuild_consultation_process_evaluation

results_dir = Path('uploads/dingtalk_staging/results')
transcripts_dir = Path('uploads/dingtalk_staging/transcripts')

fixed = 0
total = 0

for rf in sorted(results_dir.glob('*.result.json')):
    total += 1
    d = json.loads(rf.read_text())
    proc = d.get('consultation_process_evaluation', {})
    if not proc:
        continue

    # Check if any conflict exists
    has_conflict = False
    for section in proc.get('sections', []):
        for cp in section.get('checkpoints', []):
            score = float(cp.get('point_score', 0) or 0)
            issues = cp.get('issues', [])
            if issues and score > 0:
                has_conflict = True
                break
        if has_conflict:
            break
    if not has_conflict:
        continue

    # Load dialogue for evidence scanning
    sk = rf.stem.replace('.result', '')
    tf = transcripts_dir / f'{sk}.transcript.txt'
    dialogue = tf.read_text() if tf.exists() else None

    # Rebuild
    d['consultation_process_evaluation'] = rebuild_consultation_process_evaluation(d, dialogue=dialogue)
    rf.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    fixed += 1
    print(f"  Fixed: {sk[-20:]}")

print(f"\nDone: {fixed}/{total} recordings fixed")
