#!/usr/bin/env bash
set -euo pipefail

cd /opt/badge/apps/api

# The login shell sets proxy variables to 127.0.0.1:1080, which only exists
# while an SSH/VS Code Remote session is active. The API must not depend on
# that user-session proxy for DingTalk/Tencent Cloud realtime calls.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

export DINGTALK_AUDIO_SYNC_ENABLED=false
export DINGTALK_AUDIO_ARCHIVE_SYNC_ENABLED=false
export DINGTALK_AUDIO_BACKLOG_SYNC_ENABLED=false

exec /home/ymailancy/.local/bin/uv run uvicorn smart_badge_api.main:app --host 0.0.0.0 --port 8000
