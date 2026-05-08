#!/bin/bash
echo "=== api routes dir ==="
ls /opt/badge/apps/api/src/smart_badge_api/api/routes/
echo ""
echo "=== potential N+1: selectinload usage count ==="
grep -rn "selectinload\|joinedload" /opt/badge/apps/api/src/smart_badge_api/api/routes/ 2>/dev/null | wc -l
echo ""
echo "=== endpoints possibly without pagination (no offset/limit) ==="
for f in /opt/badge/apps/api/src/smart_badge_api/api/routes/*.py; do
  echo "--- $(basename $f) ---"
  grep -nE "@router\.(get|post)|page_size|\.limit\(|\.offset\(" "$f" | head -40
done 2>/dev/null
