#!/usr/bin/env bash
# Execute from an immutable, CI-validated SHA release directory on one server.
set -euo pipefail
release_dir=$(cd "$(dirname "$0")/.." && pwd)
deploy_root=/srv/zhixu
env_file=
source_sha=
validate_only=0
app_only=0
use_existing=0
while (($#)); do
  case "$1" in
    --root) deploy_root=$2; shift 2;;
    --env-file) env_file=$2; shift 2;;
    --sha) source_sha=$2; shift 2;;
    --validate-only|--dry-run) validate_only=1; shift;;
    --app-only) app_only=1; shift;;
    --use-existing-volume) use_existing=1; shift;;
    *) echo 'Unknown deployment argument' >&2; exit 2;;
  esac
done
[[ "$source_sha" =~ ^[a-f0-9]{40}$ ]] || { echo 'A validated 40-character source SHA is required' >&2; exit 2; }
[[ -f "$release_dir/SOURCE_SHA" && $(cat "$release_dir/SOURCE_SHA") == "$source_sha" ]] || { echo 'Release source SHA does not match' >&2; exit 2; }
env_file=${env_file:-$deploy_root/shared/.env}
source "$(dirname "$0")/ops_common.sh"
init_ops
export SOURCE_SHA="$source_sha"
SCHEMA_HASH=$(python3 "$ops_dir/ops_backup.py" schema "$release_dir/server.py")
export SCHEMA_HASH
export APP_IMAGE="${project}-app:$source_sha"
domain=$(read_setting WORKSPACE_DOMAIN '')
[[ "$domain" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ && "$domain" != *..* ]] || { echo 'A valid server domain is required' >&2; exit 2; }
compose config -q
bash -n "$ops_dir/backup.sh" "$ops_dir/restore.sh"
if ((validate_only)); then echo 'PASS deployment inputs and Compose configuration validated; no service changed'; exit; fi
lock_ops
managed_app
previous_image=
previous_release="$release_dir"
if [[ -L "$deploy_root/current" ]]; then previous_release=$(readlink -f "$deploy_root/current"); fi
[[ -f "$previous_release/compose.yaml" && "$previous_release" == "$deploy_root/"* || "$previous_release" == "$release_dir" ]] || { echo 'Unsafe previous release path' >&2; exit 2; }
if [[ -n "$container" ]]; then
  previous_image=$(docker inspect --format '{{.Image}}' "$container")
  previous_schema=$(docker image inspect --format '{{index .Config.Labels "io.zhixu.schema"}}' "$previous_image")
  if [[ ! "$previous_schema" =~ ^[a-f0-9]{64}$ ]]; then
    previous_schema=$(docker run --rm --network none --entrypoint python "$previous_image" -c 'import server,hashlib,importlib.util; parts=[server.SCHEMA]; parts.extend([__import__("bedtime").SCHEMA] if importlib.util.find_spec("bedtime") else []); print(hashlib.sha256("\n--module-schema--\n".join(parts).encode()).hexdigest())')
  fi
  [[ "$previous_schema" == "$SCHEMA_HASH" ]] || { echo 'Schema change refused before stopping app; reviewed migration and recovery plan required' >&2; exit 4; }
elif docker volume inspect "$data_volume" >/dev/null 2>&1 && ((!use_existing)); then
  echo 'Existing data volume has no app; require explicit --use-existing-volume after manual verification' >&2; exit 2
fi
# Build completion is required before touching the running app.
compose build app
if ((!app_only)); then compose pull gateway; fi
printf '%s\n' "$previous_image" > "$deploy_root/state/previous-image"
changed=0
completed=0
rollback() {
  status=$?
  if ((changed && !completed)); then
    if [[ -n "$previous_image" ]]; then
      export APP_IMAGE="$previous_image"
      release_dir="$previous_release"
      if compose up -d --no-build --no-deps app && wait_healthy; then
        echo 'ROLLBACK previous immutable app image is healthy; current database/uploads were preserved' >&2
      else
        echo 'ROLLBACK FAILED; keep volumes intact and inspect the saved backup manually' >&2
      fi
    else
      compose stop app >/dev/null || true
      echo 'First deployment failed; no previous image exists; data volume retained' >&2
    fi
  fi
  exit "$status"
}
wait_healthy() {
  local candidate health running
  for ((i=0;i<45;i++)); do
    candidate=$(compose ps -a -q app)
    [[ -n "$candidate" ]] || return 1
    running=$(docker inspect --format '{{.State.Running}}' "$candidate")
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$candidate")
    [[ "$running" == true ]] || return 1
    [[ "$health" != unhealthy ]] || return 1
    if [[ "$health" == healthy ]]; then return 0; fi
    sleep 2
  done
  return 1
}
trap rollback EXIT
if [[ -n "$previous_image" ]]; then
  # Backup takes the same deployment lock, and intentionally leaves app stopped.
  "$ops_dir/backup.sh" --root "$deploy_root" --env-file "$env_file" --release "$release_dir" --leave-stopped --lock-held
fi
changed=1
compose up -d --no-build --no-deps app
wait_healthy || { echo 'New app health failed' >&2; exit 5; }
compose exec -T app python -c 'import urllib.request,json; c=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/config",timeout=3)); assert c.get("testMode") is False and c.get("storagePersistence")=="persistent" and c.get("registrationMode")=="invite", "Production configuration check failed"'
if ((!app_only)); then
  compose up -d --no-build gateway
  python3 "$ops_dir/ops_health.py" --url "https://$domain" --timeout 120
fi
ln -s "$release_dir" "$deploy_root/current.next.$$"
mv -Tf "$deploy_root/current.next.$$" "$deploy_root/current"
printf '%s\n' "$source_sha" > "$deploy_root/state/current-sha"
completed=1
if ((app_only)); then echo 'PASS APP-ONLY TEST deployment; public gateway/TLS was not verified'; else echo 'PASS production deployment and read-only HTTPS verification'; fi
