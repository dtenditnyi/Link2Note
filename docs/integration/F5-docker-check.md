# G-F5 — проверка деплоя в Docker на ПК (11.11.2026)

Docker Desktop 29.8.1 (Windows). Отдельный compose-проект `link2note-test` (`scratchpad/f5/docker-compose.test.yml`), фиктивный `.env.test` (`DRY_RUN=1`, `VAULT_BACKEND=api`, токены-заглушки), фейковый Second Brain (`tests/fakes/second_brain.py` как процесс) в той же сети `link2note-test-net` под именем `secondbrain-web`. Настоящие `.env`, `data/`, Telegram не использовались.

| Проверка | Результат |
|---|---|
| Сборка образа | ✅ |
| Старт в DRY_RUN без Telegram, лог моделей/ffmpeg/очереди | ✅ |
| `digest.admin api-health` по внутренней сети → `http://secondbrain-web:8000/api/v1` | ✅ `api: ok` |
| Запись заметки через `QueuedVault` изнутри контейнера | ✅ `2026-11-11 Проверка деплоя` |
| HEALTHCHECK (heartbeat < 120 с) | ✅ `healthy` |
| Процесс завершился (SIGTERM изнутри) → `restart: unless-stopped` | ✅ перезапущен, `restarts=1` |
| `docker stop` | ✅ после B04 — 1 с, «Бот остановлен», exit 0 (до — SIGKILL через 10 с) |
| `docker kill` | ⚠️ Docker считает это ручной остановкой — `unless-stopped` не перезапускает (так устроен Docker; на сервере после перезагрузки поднимет `link2note.service`) |
| `deploy/backup.sh` на работающем и остановленном контейнере | ✅ `backups/link2note-YYYYmmdd-HHMM.db.gz`, логи в `deploy/logs/` |
| `deploy/restore.sh … --yes` | ✅ база заменена, текущая сохранена в `pre-restore-…`, healthy, данные вернулись |
| `deploy/status.sh` | ✅ контейнер, health, режим, бэкап, очередь, API, ошибки |
| `docker compose down -v` для `link2note-test` | ✅ |

Не проверено вживую (нужен Linux-сервер): `install.sh` (установка Docker из apt, `chown 1000:1000`, systemd), `update.sh` (git pull + откат образа), таймер 03:45. На ПК скрипты гонялись с `NO_SUDO=1` и переопределёнными путями (`COMPOSE_FILE`, `DATA_DIR`, `BACKUP_DIR`, `ENV_FILE`). Синтаксис всех скриптов — `bash -n`; shellcheck на ПК нет.
