#!/usr/bin/env bash
set -euo pipefail

cd /opt/badge/apps/web
pnpm build

/opt/badge/scripts/start-smart-badge-web-proxy.sh >/dev/null
