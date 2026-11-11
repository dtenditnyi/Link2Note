#!/usr/bin/env bash
# Восстановление базы из бэкапа: ./deploy/restore.sh backups/link2note-YYYYmmdd-HHMM.db.gz [--yes]
# Останавливает бота, сохраняет текущую базу (backups/pre-restore-…), подменяет, запускает.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log restore
cd "$APP_DIR"

src="${1:-}"
[ -n "$src" ] || die "Укажи файл: ./deploy/restore.sh backups/link2note-….db.gz [--yes]"
[ -f "$src" ] || die "Нет файла $src"
db="$DATA_DIR/link2note.db"

if [ "${2:-}" != "--yes" ]; then
    printf 'Заменить %s содержимым %s? Бот будет остановлен. Напиши yes: ' "$db" "$src"
    read -r answer
    [ "$answer" = "yes" ] || die "Отменено"
fi

# Сначала проверить сам бэкап.
mkdir -p "$BACKUP_DIR"
tmp="$(mktemp "$BACKUP_DIR/.restore-XXXXXX")"
trap 'rm -f "$tmp"' EXIT
case "$src" in
    *.gz) gzip -dc "$src" > "$tmp" ;;
    *) cp "$src" "$tmp" ;;
esac
head -c 16 "$tmp" | grep -q "SQLite format 3" || die "$src — не база SQLite"

info "Останавливаю бота…"
compose stop "$SERVICE" || true
if [ -f "$db" ]; then
    keep="$BACKUP_DIR/pre-restore-$(date +%Y%m%d-%H%M%S).db"
    as_root cp "$db" "$keep"
    as_root chown "$(id -u):$(id -g)" "$keep"
    ok "Текущая база сохранена: $keep"
fi
as_root rm -f "$db-wal" "$db-shm" "$db-journal"
as_root mv "$tmp" "$db"
trap - EXIT
as_root chown "$APP_UID:$APP_UID" "$db" 2>/dev/null || true
as_root chmod 600 "$db" 2>/dev/null || true
ok "База восстановлена из $src"

as_root rm -f "$DATA_DIR/heartbeat"
compose up -d "$SERVICE"
wait_healthy || die "Бот не стал healthy после восстановления — docker compose logs bot"
ok "Готово"
