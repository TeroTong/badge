#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${SMART_BADGE_WEB_PROXY_CONTAINER:-smart-badge-web-proxy}"
IMAGE_NAME="${SMART_BADGE_WEB_PROXY_IMAGE:-nginx:1.27-alpine}"
HOST_PORT="${SMART_BADGE_WEB_PROXY_PORT:-5173}"
DIST_DIR="/opt/badge/apps/web/dist"
NGINX_CONF="/opt/badge/deploy/nginx/smart-badge-web-proxy.conf"
TMUX_BIN="/usr/bin/tmux"

if [ ! -f "$DIST_DIR/index.html" ]; then
    echo "Missing frontend build output at $DIST_DIR. Run deploy-smart-badge-web-proxy.sh first." >&2
    exit 1
fi

if [ ! -f "$NGINX_CONF" ]; then
    echo "Missing nginx config at $NGINX_CONF" >&2
    exit 1
fi

if "$TMUX_BIN" has-session -t smart-badge-web 2>/dev/null; then
    "$TMUX_BIN" kill-session -t smart-badge-web >/dev/null 2>&1 || true
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

exec docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --add-host=host.docker.internal:host-gateway \
    -p "0.0.0.0:${HOST_PORT}:80" \
    -v "$DIST_DIR:/usr/share/nginx/html:ro" \
    -v "$NGINX_CONF:/etc/nginx/conf.d/default.conf:ro" \
    "$IMAGE_NAME"
