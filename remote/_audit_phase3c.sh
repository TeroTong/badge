#!/bin/bash
echo "=== look for token caching patterns ==="
grep -rln "access_token\|_token_cache\|_token_expire\|cached_token" /opt/badge/apps/api/src/smart_badge_api/dingtalk.py /opt/badge/apps/api/src/smart_badge_api/sap_push_service.py /opt/badge/apps/api/src/smart_badge_api/message_push.py 2>/dev/null | head
echo "=== dingtalk.py token func ==="
grep -n "def.*token\|access_token\|_token" /opt/badge/apps/api/src/smart_badge_api/dingtalk.py 2>/dev/null | head -20
echo "=== files in src dir ==="
ls /opt/badge/apps/api/src/smart_badge_api/*.py | head -40
echo "=== iot client / wecom token ==="
grep -rln "asyncio\.Lock\|asyncio\.Event" /opt/badge/apps/api/src/smart_badge_api/ 2>/dev/null | grep -v __pycache__ | grep -v ".bak"
echo "=== recordings.py search file_name ==="
grep -n "select.*Recording\.file_name\|file_name.*select\|in_(.*file_name" /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py | head
wc -l /opt/badge/apps/api/src/smart_badge_api/api/routes/recordings.py /opt/badge/apps/api/src/smart_badge_api/api/routes/visit_orders.py
echo "=== visit_orders.py top exports ==="
grep -n "^@router\|^async def\|^def" /opt/badge/apps/api/src/smart_badge_api/api/routes/visit_orders.py | head -20
