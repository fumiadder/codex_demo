#!/usr/bin/env bash
# Stop only this managed app and copy DB + WAL + uploads as one snapshot.
set -euo pipefail
release_dir=$(cd "$(dirname "$0")/.." && pwd)
deploy_root=/srv/zhixu
env_file=
leave_stopped=0
lock_held=0
while (($#)); do
  case "$1" in
    --root) deploy_root=$2; shift 2;;
    --env-file) env_file=$2; shift 2;;
    --release) release_dir=$2; shift 2;;
    --leave-stopped) leave_stopped=1; shift;;
    --lock-held) lock_held=1; shift;;
    *) echo 'Unknown backup argument' >&2; exit 2;;
  esac
done
env_file=${env_file:-$deploy_root/shared/.env}
source "$(dirname "$0")/ops_common.sh"
init_ops
((lock_held)) || lock_ops
compose config -q
managed_app
[[ -n "$container" ]] || { echo 'No managed app exists to back up' >&2; exit 2; }
was_running=$(docker inspect --format '{{.State.Running}}' "$container")
backup_ok=0
restart_app() { if [[ "$was_running" == true && ( "$leave_stopped" == 0 || "$backup_ok" == 0 ) ]]; then compose start app >/dev/null; fi; }
trap restart_app EXIT
image=$(docker inspect --format '{{.Image}}' "$container")
backup_path="$deploy_root/backups/$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets;print(secrets.token_hex(4))')"
mkdir -m 700 "$backup_path" "$backup_path/data"
if [[ "$was_running" == true ]]; then compose stop -t 30 app >/dev/null; fi
docker cp "$container:/data/." "$backup_path/data/"
python3 "$ops_dir/ops_backup.py" snapshot "$backup_path" "$image"
python3 "$ops_dir/ops_backup.py" verify "$backup_path"
printf '%s\n' "$backup_path" > "$deploy_root/state/latest-backup"
backup_ok=1
printf 'PASS consistent backup saved to %s\n' "$backup_path"
