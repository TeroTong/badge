#!/bin/bash
set -e
cd /opt/badge
echo "=== build web ==="
pnpm --filter @smart-badge/web build 2>&1 | tail -40
echo "=== new dist mtime ==="
stat -c '%y %n' /opt/badge/apps/web/dist/index.html
echo "=== check container mount ==="
docker inspect smart-badge-web-proxy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
echo "=== nginx in container reload (just in case) ==="
docker exec smart-badge-web-proxy nginx -s reload 2>&1 || true
echo "=== curl 5173 ==="
curl -sS -o /dev/null -w "HTTP=%{http_code}\n" http://127.0.0.1:5173/
