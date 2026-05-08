#!/bin/bash
echo "=== find recordings.py and visit_orders.py ==="
find /opt/badge/apps/api/src/smart_badge_api -name "recordings.py" -not -path "*__pycache__*" 2>/dev/null
find /opt/badge/apps/api/src/smart_badge_api -name "visit_orders.py" -not -path "*__pycache__*" 2>/dev/null
echo "=== db/session.py full ==="
cat /opt/badge/apps/api/src/smart_badge_api/db/session.py
echo "=== config.py 60-80 ==="
sed -n '55,85p' /opt/badge/apps/api/src/smart_badge_api/core/config.py
echo "=== threading.Lock occurrences in api code ==="
grep -rln "threading\.Lock\|threading\.RLock\|_token_lock\|_access_token_lock" /opt/badge/apps/api/src/smart_badge_api/ 2>/dev/null | grep -v __pycache__ | grep -v ".bak"
