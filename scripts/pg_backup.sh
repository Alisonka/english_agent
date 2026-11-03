#!/usr/bin/env bash
# Dump the english_agent database (custom format, for pg_restore) and keep the newest dumps.
#
#   scripts/pg_backup.sh                   # -> ~/backups/english_agent/english_agent-<UTC time>.dump
#   BACKUP_DIR=/srv/backups BACKUP_KEEP=30 scripts/pg_backup.sh
#
# Restore: docker compose exec -T postgres pg_restore -U english_agent -d english_agent --clean < <file>.dump
set -euo pipefail

cd "$(dirname "$0")/.."
backup_dir="${BACKUP_DIR:-$HOME/backups/english_agent}"
keep="${BACKUP_KEEP:-14}"

mkdir -p "$backup_dir"
target="$backup_dir/english_agent-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Write to a temp name first so a failed dump never counts as one of the kept backups.
docker compose exec -T postgres pg_dump -U english_agent -d english_agent -Fc > "$target.partial"
mv "$target.partial" "$target"

ls -1t "$backup_dir"/english_agent-*.dump | tail -n +"$((keep + 1))" | xargs -r rm --
echo "backup written: $target"
