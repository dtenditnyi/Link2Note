#!/usr/bin/env bash
# Онлайн-бэкап SQLite (sqlite3 backup API внутри контейнера — согласованная копия, пока бот пишет)
# → backups/link2note-YYYYmmdd-HHMM.db.gz. Хранятся последние KEEP (по умолчанию 14).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log backup
cd "$APP_DIR"

KEEP="${KEEP:-14}"
mkdir -p "$BACKUP_DIR"
name="link2note-$(date +%Y%m%d-%H%M).db.gz"
tmp_in_container="/app/data/.backup-$$.db.gz"

if [ "$(health_status)" = "missing" ] || [ -z "$(compose ps -q --status running "$SERVICE" 2>/dev/null)" ]; then
    info "Контейнер не запущен — делаю бэкап одноразовым контейнером"
    compose run --rm --no-deps -T "$SERVICE" /app/.venv/bin/python -m digest.admin backup "$tmp_in_container"
else
    compose exec -T "$SERVICE" /app/.venv/bin/python -m digest.admin backup "$tmp_in_container"
fi
as_root mv "$APP_DIR/data/$(basename "$tmp_in_container")" "$BACKUP_DIR/$name"
as_root chown "$(id -u):$(id -g)" "$BACKUP_DIR/$name"
chmod 600 "$BACKUP_DIR/$name"
ok "Бэкап: backups/$name ($(du -h "$BACKUP_DIR/$name" | cut -f1))"

# Ротация: оставить KEEP самых свежих.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/link2note-*.db.gz 2>/dev/null | tail -n +"$((KEEP + 1))")
if [ "${#old[@]}" -gt 0 ]; then
    rm -f -- "${old[@]}"
    info "Удалил старых бэкапов: ${#old[@]}"
fi
