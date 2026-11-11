# Link2Note на домашнем сервере

Сервер: Proxmox → VM Ubuntu Server 24.04, рядом с сайтом Second Brain. Бот работает 24/7 в Docker, перезапускается сам, раз в сутки делает бэкап базы.

Два режима хранилища (`VAULT_BACKEND`):
- **`git`** (по умолчанию) — бот сам держит клон `myObsidian` и делает commit/push. Работает без сайта.
- **`api`** — заметки пишет сайт Second Brain через свой API; бот шлёт ему готовый Markdown. Сайт выключен — записи ждут в офлайн-очереди бота (`/queue`) и уйдут сами.

---

## 1. Требования к VM

| Что | Сколько |
|---|---|
| CPU | 2 vCPU (Whisper `small` на CPU расшифровывает ~1 мин звука за 20–40 с) |
| RAM | **+1,5–2 ГБ** к тому, что ест Second Brain (Whisper `small` int8 ≈ 1 ГБ на время расшифровки, Python/aiogram ≈ 150 МБ) |
| Диск | ~3 ГБ: образ ≈ 1,2 ГБ, модель Whisper ≈ 500 МБ, база и бэкапы — мегабайты |
| Сеть | исходящий интернет (Telegram, Gemini, YouTube/Instagram); входящие порты **не нужны** |

## 2. Клонирование

```bash
cd ~
git clone https://github.com/dtenditnyi/Link2Note.git link2note
cd link2note
```

## 3. `.env`

`.env` лежит рядом с `docker-compose.yml`, права `600` (install.sh выставит сам). Никогда не коммитить.

```ini
# --- обязательно ---
BOT_TOKEN=123456:ABC…            # @BotFather
OWNER_ID=123456789               # мой Telegram ID (@userinfobot) — бот отвечает только ему
GEMINI_API_KEY=…                 # ключ отдельного проекта AI Studio для бота

# --- куда писать заметки ---
VAULT_BACKEND=git                # git | api

# режим git: клон хранилища + deploy key
VAULT_REPO=/vault
GIT_SSH_COMMAND=ssh -i /secrets/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new

# режим api: сайт Second Brain по внутренней docker-сети
SECOND_BRAIN_API_TOKEN=…         # выдать на сайте: права write в папку бота, «Входящие», задачи; delete — только папка бота; ai
# SECOND_BRAIN_API_URL=http://secondbrain-web:8000/api/v1   # по умолчанию собирается из SECOND_BRAIN_CONTAINER
# SECOND_BRAIN_CONTAINER=secondbrain-web                    # имя контейнера сайта
# DOCKER_NETWORK=secondbrain                                # общая сеть с сайтом

# --- необязательно ---
# COOKIES_FILE=/secrets/instagram_cookies.txt
```

Все переменные (`config.py`; в скобках — значение по умолчанию):

| Переменная | Что |
|---|---|
| `BOT_TOKEN` | токен бота (обязательно) |
| `OWNER_ID` | Telegram ID владельца (обязательно) |
| `GEMINI_API_KEY` | ключ Gemini (без него конспекты и разбор голосовых не работают) |
| `GEMINI_MODEL` | основная модель (`gemini-3.5-flash-lite`, 15 RPM / 500 RPD) |
| `GEMINI_FALLBACK_MODEL` | запасная при 429/5xx (`gemini-3.1-flash-lite`; пусто — выключено) |
| `GEMINI_DETAIL_MODEL` | модель для «🔁 Подробнее» (`gemini-3.8-flash`, 20 запросов/сутки; при перегрузке — основная; пусто — основная) |
| `GEMINI_TIMEOUT_S` | таймаут запроса конспекта (`300`) |
| `TZ` | часовой пояс (`Europe/Bucharest`) |
| `DB_PATH` | база SQLite (`data/link2note.db`) |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | `small` / `cpu` / `int8` |
| `VOICE_MAX_SECONDS` | голосовые длиннее не разбираются (`120`) |
| `WHISPER_FALLBACK_MAX_SECONDS` | запасной путь конспекта через Whisper не длиннее (`1800`) |
| `VAULT_BACKEND` | `git` или `api` (`git`) |
| `VAULT_REPO` | путь к клону хранилища внутри контейнера (`/vault`; только `git`) |
| `VAULT_FOLDER` | папка конспектов (`03 - Resources/📥 Из ссылок`) |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` | автор коммитов бота (`Link2Note` / `link2note@localhost`) |
| `SECOND_BRAIN_API_URL` | база API (`http://secondbrain-web:8000/api/v1`) |
| `SECOND_BRAIN_API_TOKEN` | токен бота на сайте (обязательно при `api`) |
| `SECOND_BRAIN_TIMEOUT_S` | таймаут запроса к сайту (`15`; `/ask` — 90) |
| `OUTBOX_MAX_AGE` | сколько операция ждёт сайт, потом `dead` + сообщение (`7d`; можно `12h`, `30m`) |
| `OUTBOX_MAX_ATTEMPTS` | максимум попыток одной операции (`200` ≈ 7 суток ежечасных повторов; что наступит раньше — это или `OUTBOX_MAX_AGE`) |
| `COOKIES_FILE` | cookies.txt для Instagram (пусто — без входа) |
| `VIDEO_MAX_SECONDS` / `VIDEO_MAX_MB` | лимиты скачивания видео (`3600` / `300`) |
| `WEEKLY_DIGEST` | воскресный список непрочитанного в 18:00 (`true`) |
| `DRY_RUN` | `1` — без Telegram: только база, очередь сайта, heartbeat (проверка контейнера) |

Переменные compose (не Python): `DOCKER_NETWORK` (`secondbrain`), `SECOND_BRAIN_CONTAINER` (`secondbrain-web`), `LINK2NOTE_CONTAINER` (`link2note`).

### Как узнать имя контейнера сайта и сеть
```bash
docker ps --format '{{.Names}}\t{{.Networks}}'      # имя контейнера Second Brain и его сети
docker network ls                                    # есть ли сеть secondbrain
```
Если сайт в другой сети — либо `DOCKER_NETWORK=<его сеть>`, либо подключить сайт к `secondbrain`: `docker network connect secondbrain <контейнер-сайта>` (лучше прописать сеть в compose сайта как `external`). Проверка изнутри бота: `docker compose exec bot /app/.venv/bin/python -m digest.admin api-health`.

### Режим git: клон и deploy key
```bash
ssh-keygen -t ed25519 -N '' -f secrets/deploy_key -C link2note
cat secrets/deploy_key.pub    # GitHub → myObsidian → Settings → Deploy keys → Add, ✅ Allow write access
GIT_SSH_COMMAND="ssh -i secrets/deploy_key -o IdentitiesOnly=yes" git clone git@github.com:Lol4ik0/myObsidian.git vault
sudo chown -R 1000:1000 vault secrets && chmod 600 secrets/deploy_key
```

### Cookies Instagram (необязательно)
Экспортировать cookies.txt (формат Netscape) расширением браузера **со второго аккаунта** → `secrets/instagram_cookies.txt`, в `.env` — `COOKIES_FILE=/secrets/instagram_cookies.txt`. Бот берёт копию файла (yt-dlp перезаписывает его при закрытии).

## 4. Установка

```bash
./deploy/install.sh
```
Скрипт идемпотентный: ставит Docker Engine + compose plugin из официального репозитория (если Docker уже стоит после Second Brain — пропускает), создаёт `data/ vault/ secrets/ backups/` с владельцем 1000:1000, проверяет `.env` для выбранного режима (значения не печатает), создаёт сеть `secondbrain`, собирает образ, запускает, ждёт `healthy`, ставит systemd-юниты:
- `link2note.service` — поднимает compose при загрузке;
- `link2note-backup.timer` — бэкап каждый день в **03:45** (бэкап Second Brain — в 03:30).
Лог каждого запуска — `deploy/logs/`.

Заметки по установке:
- Docker только что поставлен и ты не root → скрипт добавит тебя в группу `docker` и остановится: **перелогинься и запусти `install.sh` ещё раз**.
- Таймер бэкапа задан по `Europe/Bucharest` прямо в `OnCalendar` — от пояса VM (у Ubuntu по умолчанию UTC) не зависит.
- `install.sh` создаёт сеть `secondbrain`, если её нет. В compose сайта её лучше тоже объявить `external: true` — иначе compose сайта предупредит, что сеть создана не им.

## 5. Проверка

```bash
./deploy/status.sh                 # контейнер, health, очередь сайта, последний бэкап, режим, API, ошибки
docker compose logs -f bot         # живой лог
```
В Telegram: `/help`, ссылка на короткое видео → конспект, «✅ Прочитано». В режиме api — ещё `/task тест`, `/queue`.

**Healthcheck:** бот раз в 30 с пишет `data/heartbeat` из своего event loop; `HEALTHCHECK` в Dockerfile считает контейнер больным, если файл старше 120 с (завис loop). `restart: unless-stopped` перезапускает упавший процесс; зависший, но живой процесс Docker сам не перезапускает — его видно в `status.sh` как `unhealthy`.

## 6. Переключение git → api

1. На сайте выдать токен бота (права из раздела 3) и проверить контракт **на песочнице**:
   ```bash
   export SECOND_BRAIN_API_URL=http://<адрес-сайта>/api/v1
   read -rs SECOND_BRAIN_API_TOKEN && export SECOND_BRAIN_API_TOKEN   # токен не попадёт в историю shell
   uv run python scripts/check_second_brain_api.py                  # или внутри контейнера бота
   ```
   Скрипт создаёт заметку в `03 - Resources/📥 Из ссылок/_contract-test`, меняет статус, заменяет, ищет по url, удаляет, проверяет идемпотентность и коды ошибок; печатает ✅/❌. Всё ✅ — можно переключать.
2. Дождаться, пока git-режим всё отправил: в логе нет «не отправлена в GitHub», `cd vault && git status` — чисто.
3. В `.env`: `VAULT_BACKEND=api`, `SECOND_BRAIN_API_TOKEN=…` → `docker compose up -d`.
4. Кнопки под старыми конспектами продолжают работать: сайт ищет заметку по имени или по `url:`.

Как ведёт себя очередь: «✅ Прочитано» и «🗑 Удалить» при лежащем сайте сразу меняют кнопки и базу бота, а на сайт уходят позже. Если операция в итоге не дойдёт (`dead`, например 403), придёт сообщение «❌ Не смог отправить…» — тогда `/retry` или поправить заметку вручную; правда о «прочитано» всё равно во frontmatter файла.

**Непустая очередь при обратном переключении (api → git):** операции в `vault_ops` в режиме git не отправляются. Сначала `/queue` → `/retry`, пока очередь не пуста (или дождаться), и только потом переключать. Если сайт умер насовсем — ждущие заметки видны в `/queue`; их Markdown лежит в базе (`vault_ops.payload`), но автоматически в git не перекладывается.

## 7. Обновление

```bash
./deploy/update.sh
```
`git pull --ff-only` → бэкап → сборка → запуск → ждёт `healthy`. Не поднялся — откат на предыдущий образ (`link2note:rollback`) и сообщение с командой отката кода.

## 8. Бэкапы и восстановление

- Ручной бэкап: `./deploy/backup.sh` → `backups/link2note-YYYYmmdd-HHMM.db.gz` (онлайн-копия через sqlite3 backup API внутри контейнера, проверка `integrity_check`), хранятся 14 последних.
- Восстановление: `./deploy/restore.sh backups/link2note-….db.gz` → подтверждение `yes` → бот останавливается, текущая база сохраняется в `backups/pre-restore-…`, подменяется, бот запускается.
- Расписание: `systemctl list-timers link2note-backup.timer`.

## 9. Перенос базы с Windows-ПК на сервер

На ПК (бот остановлен, Ctrl+C):
```powershell
uv run python -m digest.admin --db data/link2note.db backup link2note-from-pc.db.gz
scp link2note-from-pc.db.gz user@server:~/link2note/backups/
```
На сервере:
```bash
./deploy/restore.sh backups/link2note-from-pc.db.gz
```
Напоминания, дубликаты, кнопки под конспектами и очередь ссылок переедут вместе с базой. Не запускать бота на ПК и на сервере одновременно с одним токеном — Telegram отдаёт обновления только одному.

## 10. Логи

- `docker compose logs --tail 200 bot`, `docker compose logs -f bot`.
- Ротация: `json-file`, 5 файлов по 10 МБ.
- Токены в лог не пишутся (ошибки конфигурации печатаются без значений).

## 11. Чек-лист «бот молчит»

1. `./deploy/status.sh` — контейнер `running` и `healthy`?
2. `docker compose logs --tail 100 bot` — `Unauthorized` → неверный `BOT_TOKEN`; `TelegramConflictError` → бот запущен ещё где-то (ПК!).
3. Пишешь не со своего аккаунта? Бот отвечает только `OWNER_ID` (в логе «Ignored update from …»).
4. Ссылки не принимаются: git — `vault/` не клон (в логе «не git-репозиторий»); api — смотри `/queue` и `status.sh` → «Сайт Second Brain».
5. `unhealthy` — event loop завис: `docker compose restart bot`, лог перед зависанием — в issue.
6. Конспекты «Gemini перегружен» — лимит free tier; задача отложится сама.
7. После перезагрузки VM бот не поднялся: `systemctl status link2note.service docker`.
