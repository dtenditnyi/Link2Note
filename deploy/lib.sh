#!/usr/bin/env bash
# Общие функции deploy-скриптов Link2Note. Подключается через `source`.
# Каждый скрипт пишет свой лог в deploy/logs/<скрипт>-YYYYmmdd-HHMMSS.log.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Пути можно переопределить окружением (проверка скриптов на тестовом compose-проекте).
LOG_DIR="${LOG_DIR:-$APP_DIR/deploy/logs}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
SERVICE="bot"
APP_UID=1000
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-300}"

start_log() {
    mkdir -p "$LOG_DIR"
    LOG_FILE="$LOG_DIR/$1-$(date +%Y%m%d-%H%M%S).log"
    exec > >(tee -a "$LOG_FILE") 2>&1
    info "Лог: $LOG_FILE"
}

info() { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

as_root() {
    # NO_SUDO=1 — проверка скриптов без root (тестовый compose-проект на ПК).
    if [ "$(id -u)" -eq 0 ] || [ "${NO_SUDO:-0}" = 1 ] || ! command -v sudo >/dev/null 2>&1; then
        "$@"
    else
        sudo "$@"
    fi
}

compose() {
    (cd "$APP_DIR" && docker compose "$@")
}

# Значение переменной из .env без вывода на экран (последнее присваивание, без кавычек).
env_get() {
    local key="$1" line
    [ -f "$ENV_FILE" ] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%$'\r'}"
    line="${line#\"}"; line="${line%\"}"
    line="${line#\'}"; line="${line%\'}"
    printf '%s' "$line"
}

container_name() {
    local name
    name="$(env_get LINK2NOTE_CONTAINER)"
    printf '%s' "${LINK2NOTE_CONTAINER:-${name:-link2note}}"
}

health_status() {
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$(container_name)" 2>/dev/null || echo "missing"
}

# Ждать healthy (heartbeat бота свежий). 0 — дождались, 1 — нет.
wait_healthy() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S)) status
    info "Жду, пока контейнер станет healthy (до ${HEALTH_TIMEOUT_S} с)…"
    while [ "$SECONDS" -lt "$deadline" ]; do
        status="$(health_status)"
        case "$status" in
            healthy) ok "Контейнер healthy"; return 0 ;;
            unhealthy|exited|dead|missing) warn "Состояние: $status"; return 1 ;;
        esac
        sleep 5
    done
    warn "Не дождался healthy, последнее состояние: $(health_status)"
    return 1
}
