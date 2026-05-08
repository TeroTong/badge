#!/bin/bash
echo "=== docker ps ==="
docker ps 2>/dev/null | head
echo "=== nginx web conf ==="
grep -rl "badge\|smart" /etc/nginx/ 2>/dev/null | head
echo "--- default ---"
cat /etc/nginx/conf.d/default 2>/dev/null
echo "=== badge top-level files ==="
ls /opt/badge/
echo "=== pnpm path ==="
which pnpm; which uv
echo "=== last build mtime ==="
stat -c '%y %n' /opt/badge/apps/web/dist /opt/badge/apps/web/dist/index.html 2>/dev/null
