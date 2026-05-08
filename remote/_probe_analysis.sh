#!/bin/bash
cd /opt/badge/apps/api
/home/ymailancy/.local/bin/uv run python << 'PY'
from smart_badge_api.core.config import get_settings
import os, time
s = get_settings()
p = s.results_path
print("results_dir=", p, "exists=", p.exists())
files = sorted(p.glob('*.result.json'))
print("count=", len(files))
sizes = [f.stat().st_size for f in files]
if sizes:
    sizes_sorted = sorted(sizes)
    print(f"total_mb={sum(sizes)/1024/1024:.1f} median_kb={sizes_sorted[len(sizes)//2]//1024} max_kb={max(sizes)//1024}")

# Try loading one
from smart_badge_api.api.routes.analysis import _load_cached_analysis_result_summaries, clear_analysis_result_list_cache
clear_analysis_result_list_cache()

import asyncio
from smart_badge_api.db.session import async_session_factory
async def main():
    async with async_session_factory() as db:
        t0 = time.monotonic()
        items = await _load_cached_analysis_result_summaries(db)
        dt = time.monotonic() - t0
        print(f"loaded {len(items)} summaries in {dt:.2f}s")
asyncio.run(main())
PY
