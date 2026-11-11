#!/usr/bin/env bash
# Состояние Link2Note: контейнер, health, ошибки в логах за сутки, очередь сайта, бэкапы, API.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log status
cd "$APP_DIR"

backend="$(env_get VAULT_BACKEND)"
backend="${backend:-git}"
name="$(container_name)"
state="$(docker inspect --format '{{.State.Status}} (с {{.State.StartedAt}}, рестартов {{.RestartCount}})' "$name" 2>/dev/null || echo 'нет контейнера')"

echo "== Link2Note =="
echo "Контейнер:     $name — $state"
echo "Health:        $(health_status)"
echo "VAULT_BACKEND: $backend"
echo "Версия кода:   $(git rev-parse --short HEAD 2>/dev/null || echo '?')"

last="$(ls -1t "$BACKUP_DIR"/link2note-*.db.gz 2>/dev/null | head -n 1 || true)"
if [ -n "$last" ]; then
    echo "Бэкап:         $(basename "$last") ($(date -r "$last" '+%d.%m.%Y %H:%M'))"
else
    echo "Бэкап:         нет ни одного — ./deploy/backup.sh"
fi

running=0
[ -n "$(compose ps -q --status running "$SERVICE" 2>/dev/null)" ] && running=1

echo
echo "== Очередь сайта (vault_ops) =="
if [ "$running" -eq 1 ]; then
    compose exec -T "$SERVICE" /app/.venv/bin/python -m digest.admin queue || warn "не смог прочитать очередь"
else
    warn "контейнер не запущен"
fi

echo
echo "== Сайт Second Brain =="
if [ "$backend" = "api" ] && [ "$running" -eq 1 ]; then
    compose exec -T "$SERVICE" /app/.venv/bin/python -m digest.admin api-health || true
else
    echo "не используется (VAULT_BACKEND=$backend) или бот не запущен"
fi

echo
echo "== Ошибки в логах за 24 ч (последние 20) =="
compose logs --no-color --since 24h "$SERVICE" 2>/dev/null \
    | grep -E "ERROR|CRITICAL|Traceback" | tail -n 20 || echo "нет"
