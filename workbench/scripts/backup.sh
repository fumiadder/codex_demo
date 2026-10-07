#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "backups/$stamp"
docker compose stop app
trap 'docker compose start app >/dev/null' EXIT
docker compose cp app:/data/. "backups/$stamp/"
printf '备份已保存：%s/backups/%s\n' "$PWD" "$stamp"
