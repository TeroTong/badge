#!/bin/bash
echo "=== database.py ==="
find /opt/badge/apps/api/src/smart_badge_api -name "database.py" -o -name "db.py" -o -name "session.py" 2>/dev/null | head
echo "=== config related ==="
find /opt/badge/apps/api/src/smart_badge_api -name "config.py" -o -name "settings.py" 2>/dev/null | head
echo "=== look for create_async_engine pool ==="
grep -rn "create_async_engine\|pool_size\|max_overflow" /opt/badge/apps/api/src/smart_badge_api/ 2>/dev/null | grep -v __pycache__ | head -30
echo "=== recordings.py:2120-2140 ==="
sed -n '2110,2150p' /opt/badge/apps/api/src/smart_badge_api/api/v1/recordings.py 2>/dev/null
echo "=== visit_orders.py:370-440 ==="
sed -n '370,440p' /opt/badge/apps/api/src/smart_badge_api/api/v1/visit_orders.py 2>/dev/null
echo "=== files with threading.Lock for tokens ==="
grep -ln "threading\.Lock\|threading\.RLock" /opt/badge/apps/api/src/smart_badge_api/dingtalk.py /opt/badge/apps/api/src/smart_badge_api/sap_push_service.py /opt/badge/apps/api/src/smart_badge_api/message_push.py /opt/badge/apps/api/src/smart_badge_api/asr/tencent_cloud_provider.py /opt/badge/apps/api/src/smart_badge_api/asr/xfyun_asr_provider.py 2>/dev/null
