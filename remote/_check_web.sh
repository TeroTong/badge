#!/bin/bash
echo "=== web dir ==="
ls /opt/badge/apps/web/
echo "=== package.json scripts ==="
cat /opt/badge/apps/web/package.json | head -40
echo "=== nginx? ==="
ls /etc/nginx/conf.d/ 2>/dev/null
ls /etc/nginx/sites-enabled/ 2>/dev/null
echo "=== running web procs ==="
ps -ef | grep -E "vite|node.*web|nginx" | grep -v grep | head -10
echo "=== dist? ==="
ls /opt/badge/apps/web/dist 2>/dev/null | head -5
stat -c '%y' /opt/badge/apps/web/dist 2>/dev/null
echo "=== pm2/systemd? ==="
systemctl list-units --type=service 2>/dev/null | grep -iE "badge|web|vite|node" | head
