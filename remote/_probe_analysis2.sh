#!/bin/bash
cd /opt/badge/apps/api
/home/ymailancy/.local/bin/uv run python << 'PY'
import time, json
from pathlib import Path
from smart_badge_api.core.config import get_settings
from smart_badge_api.api.analysis_normalization import normalize_analysis_result
from smart_badge_api.analysis.pipeline import sanitize_analysis_result_with_raw

p = get_settings().results_path
files = sorted(p.glob('*.result.json'))[:50]
t0 = time.monotonic()
for fp in files:
    with open(fp, encoding='utf-8') as f:
        data = json.load(f)
print(f"plain load 50 files: {time.monotonic()-t0:.2f}s")

t0 = time.monotonic()
for fp in files:
    with open(fp, encoding='utf-8') as f:
        data = json.load(f)
    data = normalize_analysis_result(data) or {}
print(f"+normalize 50 files: {time.monotonic()-t0:.2f}s")

t0 = time.monotonic()
upload_dir = get_settings().upload_path
for fp in files:
    with open(fp, encoding='utf-8') as f:
        data = json.load(f)
    file_id = fp.name.replace('.result.json','')
    raw = None
    for cand in (upload_dir / f"{file_id}.json", upload_dir / "analysis_input" / f"{file_id}.json"):
        if cand.exists():
            with open(cand, encoding='utf-8') as f:
                raw = json.load(f)
            break
    if raw:
        sanitize_analysis_result_with_raw(data, raw=raw)
    data = normalize_analysis_result(data) or {}
print(f"+sanitize+normalize 50 files: {time.monotonic()-t0:.2f}s")
PY
