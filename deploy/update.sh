#!/usr/bin/env bash
# Обновление: git pull → бэкап базы → сборка → запуск → ждём healthy.
# Не стал healthy — откат на предыдущий образ (link2note:rollback) и сообщение.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log update
cd "$APP_DIR"

before="$(git rev-parse --short HEAD)"
info "Текущая версия: $before"
git pull --ff-only
after="$(git rev-parse --short HEAD)"
[ "$before" = "$after" ] && info "Код не изменился — всё равно пересоберу" || ok "Обновлено: $before → $after"

"$APP_DIR/deploy/backup.sh" || warn "Бэкап не удался — продолжаю обновление"

had_image=0
if docker image inspect link2note:latest >/dev/null 2>&1; then
    docker image tag link2note:latest link2note:rollback
    had_image=1
    ok "Текущий образ сохранён как link2note:rollback"
fi

info "Собираю новый образ…"
# heartbeat удаляем прямо перед заменой контейнера: healthy — только по свежему файлу нового.
if compose build && as_root rm -f "$DATA_DIR/heartbeat" && compose up -d && wait_healthy; then
    ok "Обновление готово: $after"
    exit 0
fi

warn "Новая версия не поднялась"
if [ "$had_image" -eq 1 ]; then
    warn "Откатываюсь на предыдущий образ…"
    docker image tag link2note:rollback link2note:latest
    as_root rm -f "$DATA_DIR/heartbeat"
    compose up -d --no-build --force-recreate
    if wait_healthy; then
        die "Откатился на предыдущий образ (код остался $after — разберись: docker compose logs bot; git checkout $before)"
    fi
fi
die "Бот не работает — смотри docker compose logs --tail 200 bot"
