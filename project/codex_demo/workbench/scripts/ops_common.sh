#!/usr/bin/env bash
# Shared by local server operations. Do not source .env as executable shell.
set -euo pipefail
umask 077
ops_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
read_setting() {
  python3 "$ops_dir/ops_backup.py" env "$env_file" "$1" "$2"
}
init_ops() {
  [[ "$deploy_root" = /* && "$deploy_root" != / && ! "$deploy_root" =~ (^|/)\.\.(/|$) ]] || { echo 'Invalid absolute deployment root' >&2; exit 2; }
  [[ -f "$env_file" ]] || { echo 'Missing server environment file' >&2; exit 2; }
  project=$(read_setting COMPOSE_PROJECT_NAME zhixu)
  data_volume=$(read_setting WORKSPACE_DATA_VOLUME "${project}_data")
  [[ "$project" =~ ^[a-z0-9][a-z0-9_-]{0,47}$ && "$data_volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$ ]] || { echo 'Invalid stable project/volume name' >&2; exit 2; }
  export COMPOSE_PROJECT_NAME="$project" WORKSPACE_DATA_VOLUME="$data_volume"
  mkdir -p "$deploy_root/state" "$deploy_root/backups"
  [[ ! -L "$deploy_root/state" && ! -L "$deploy_root/backups" ]] || { echo 'State/backup directories must not be symlinks' >&2; exit 2; }
}
compose() { docker compose --env-file "$env_file" --project-name "$project" --project-directory "$release_dir" -f "$release_dir/compose.yaml" "$@"; }
lock_ops() { exec 9>"$deploy_root/state/deploy.lock"; flock -n 9 || { echo 'Another deployment/backup is active' >&2; exit 3; }; }
managed_app() {
  container=$(compose ps -a -q app)
  if [[ -n "$container" ]]; then
    [[ $(docker inspect --format '{{index .Config.Labels "io.zhixu.managed"}}' "$container") == true ]] || { echo 'Refusing to modify an unmanaged app container' >&2; exit 2; }
  fi
}
