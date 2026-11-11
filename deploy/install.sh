#!/usr/bin/env bash
# Первая установка Link2Note на сервер (Ubuntu 24.04). Идемпотентно: можно запускать повторно.
#   ./deploy/install.sh            — всё: Docker, папки, сеть, проверка .env, сборка, запуск, systemd
#   SKIP_SYSTEMD=1 ./deploy/install.sh — без установки systemd-юнитов
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
start_log install
cd "$APP_DIR"

# 1. Docker Engine + compose plugin (официальный репозиторий Docker). Уже стоит — пропускаем.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker уже установлен: $(docker --version)"
else
    info "Ставлю Docker Engine из официального репозитория…"
    [ -r /etc/os-release ] || die "Не вижу /etc/os-release — поставь Docker вручную"
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${ID:-}" = "ubuntu" ] || warn "Система $ID, скрипт рассчитан на Ubuntu"
    as_root apt-get update
    as_root apt-get install -y ca-certificates curl
    as_root install -m 0755 -d /etc/apt/keyrings
    as_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    as_root chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
        | as_root tee /etc/apt/sources.list.d/docker.list >/dev/null
    as_root apt-get update
    as_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    as_root systemctl enable --now docker
    if [ "$(id -u)" -ne 0 ]; then
        as_root usermod -aG docker "$USER" || true
        warn "Пользователь $USER добавлен в группу docker — перелогинься, чтобы работать без sudo"
    fi
    ok "Docker установлен"
fi
docker info >/dev/null 2>&1 || die "Docker не отвечает (нет прав? перелогинься после добавления в группу docker)"

# 2. Папки с владельцем 1000:1000 (botuser в контейнере).
mkdir -p data vault secrets backups deploy/logs
as_root chown "$APP_UID:$APP_UID" data vault
as_root chown -R "$APP_UID:$APP_UID" secrets
as_root chmod 700 secrets
chmod 700 backups
ok "Папки data/ vault/ secrets/ backups/ готовы"

# 3. .env: обязательные переменные для выбранного VAULT_BACKEND (значения не печатаем).
[ -f "$ENV_FILE" ] || die ".env не найден — создай по образцу из docs/DEPLOY.md"
chmod 600 "$ENV_FILE"
missing=()
for key in BOT_TOKEN OWNER_ID GEMINI_API_KEY; do
    [ -n "$(env_get "$key")" ] || missing+=("$key")
done
backend="$(env_get VAULT_BACKEND)"
backend="${backend:-git}"
case "$backend" in
    git)
        [ -n "$(env_get VAULT_REPO)" ] || missing+=("VAULT_REPO")
        if [ ! -d vault/.git ]; then
            warn "vault/ — не git-клон: бот не будет принимать ссылки (см. docs/DEPLOY.md, шаг «git»)"
        fi
        [ -n "$(env_get GIT_SSH_COMMAND)" ] || warn "GIT_SSH_COMMAND не задан — push по ssh не сработает"
        ;;
    api)
        [ -n "$(env_get SECOND_BRAIN_API_TOKEN)" ] || missing+=("SECOND_BRAIN_API_TOKEN")
        ;;
    *) die "VAULT_BACKEND=$backend — должно быть git или api" ;;
esac
[ "${#missing[@]}" -eq 0 ] || die "В .env не заданы: ${missing[*]}"
ok ".env в порядке (VAULT_BACKEND=$backend)"

# 4. Общая сеть с Second Brain.
network="${DOCKER_NETWORK:-$(env_get DOCKER_NETWORK)}"
network="${network:-secondbrain}"
if docker network inspect "$network" >/dev/null 2>&1; then
    ok "Сеть $network уже есть"
else
    docker network create "$network" >/dev/null
    ok "Создал сеть $network"
fi

# 5. Сборка и запуск.
info "Собираю образ…"
compose build
compose up -d
wait_healthy || die "Бот не стал healthy — смотри: docker compose logs --tail 100 bot"

# 6. systemd: автозапуск и ежедневный бэкап 03:45.
if [ "${SKIP_SYSTEMD:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
    for unit in link2note.service link2note-backup.service link2note-backup.timer; do
        sed "s#@APP_DIR@#$APP_DIR#g" "deploy/systemd/$unit" | as_root tee "/etc/systemd/system/$unit" >/dev/null
    done
    as_root systemctl daemon-reload
    as_root systemctl enable link2note.service link2note-backup.timer
    as_root systemctl start link2note-backup.timer
    ok "systemd: link2note.service и link2note-backup.timer включены"
else
    warn "systemd-юниты пропущены"
fi

echo
ok "Link2Note установлен: контейнер $(container_name) — $(health_status), VAULT_BACKEND=$backend"
info "Проверка: ./deploy/status.sh   Логи: docker compose logs -f bot"
