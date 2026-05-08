#!/bin/bash
cd /opt/badge/apps/api
/home/ymailancy/.local/bin/uv run python << 'PY'
from smart_badge_api.core.config import get_settings
import os
s = get_settings()
p = s.upload_path
print("dir=", p)
print("exists=", p.exists())
files = [f for f in os.listdir(p) if f.endswith('.result.json')] if p.exists() else []
print("count=", len(files))
if files:
    sizes = [os.path.getsize(p / f) for f in files]
    print("total_kb=", sum(sizes) // 1024, "median_kb=", sorted(sizes)[len(sizes)//2] // 1024)
PY
