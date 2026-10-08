#!/usr/bin/env bash
# Restore into a NEW volume only. Never switches or overwrites production data.
set -euo pipefail
umask 077
backup_path=
new_volume=
image=
while (($#)); do
  case "$1" in
    --backup) backup_path=$2; shift 2;;
    --new-volume) new_volume=$2; shift 2;;
    --image) image=$2; shift 2;;
    --validate-only) validate_only=1; shift;;
    *) echo 'Unknown restore argument' >&2; exit 2;;
  esac
done
[[ -d "$backup_path" && "$new_volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$ ]] || { echo 'Specify a backup directory and safe new volume name' >&2; exit 2; }
ops_dir=$(cd "$(dirname "$0")" && pwd)
python3 "$ops_dir/ops_backup.py" verify "$backup_path"
[[ ${validate_only:-0} == 0 ]] || { echo 'PASS restore inputs validated; Docker not changed'; exit; }
[[ -n "$image" ]] || image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["image"])' "$backup_path/manifest.json")
docker image inspect "$image" >/dev/null
if docker volume inspect "$new_volume" >/dev/null 2>&1; then echo 'Refusing to overwrite an existing volume' >&2; exit 2; fi
docker volume create --label io.zhixu.restore=true "$new_volume" >/dev/null
helper="zhixu-restore-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
trap 'docker rm -f "$helper" >/dev/null 2>&1 || true' EXIT
check='import os,sqlite3,pathlib; p=pathlib.Path("/data"); c=sqlite3.connect(p/"folio.sqlite3"); assert c.execute("PRAGMA quick_check").fetchone()[0]=="ok"; tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type=\"table\"")}; assert all((p/folder/i).is_file() and (p/folder/i).stat().st_size==size for table,folder in [("files","uploads"),("bedtime_audio","bedtime-audio")] if table in tables for i,size in c.execute("SELECT id,size FROM "+table)); c.close(); [os.chown(x,10001,10001) for x in [p,*p.rglob("*")]]; os.chmod(p,0o700); print("PASS restored SQLite and upload references")'
docker create --name "$helper" --network none --user 0 --mount "type=volume,source=$new_volume,target=/data" --entrypoint python "$image" -c "$check" >/dev/null
docker cp "$backup_path/data/." "$helper:/data/"
docker start -a "$helper"
printf 'PASS restored to new volume %s; production has NOT been switched\n' "$new_volume"
