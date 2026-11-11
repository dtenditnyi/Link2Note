# Link2Note — что сделать руками (готовые команды)

Порядок важен. ПК = Windows (PowerShell в папке `C:\Users\danylo\Desktop\Projects\Link2Note\Link2Note`), сервер = Ubuntu (bash).

---

## 1. На ПК: правила автономии + слияние в main

Claude Code не смог сам изменить свои инструкции (защита), правки лежат в `git stash`. Применяем их на рабочей ветке и сливаем всё в `main`.

```powershell
cd C:\Users\danylo\Desktop\Projects\Link2Note\Link2Note
git checkout feature/second-brain-integration
git pull
git stash list                      # должна быть строка: stash@{0}: On main: stage5: autonomous workflow docs…
git stash pop                       # применяет правки CLAUDE.md и docs/WORKFLOW.md
git status                          # если написано "both modified" / CONFLICT — см. примечание ниже
```

Теперь вручную поправь skill — открой файл в VS Code:
```powershell
code .claude\skills\feature\SKILL.md
```
Замени:
- в строке `description:` — `коммит после подтверждения` → `коммит сам при зелёных тестах`;
- в пункте 3 — `**Остановись и жди моего «ок».**` → `**Выбери вариант сам и запиши решение в DECISIONS.md.**`;
- в пункте 7 — `Предложи сообщение коммита на английском и коммить только после моего подтверждения.` → `Коммить сам при зелёных тестах + ruff + ревью, сообщение на английском.`

Сохрани и закоммить, проверь тесты, слей в `main`:
```powershell
git add CLAUDE.md docs/WORKFLOW.md .claude/skills/feature/SKILL.md
git commit -m "docs: autonomous workflow for Claude Code"
uv run pytest -q                    # должно быть ~863 passed
git checkout main
git pull
git merge --no-ff feature/second-brain-integration -m "Merge stage 5: home server + Second Brain API integration"
git push origin main
```

> **Если `git stash pop` дал конфликт:** открой `CLAUDE.md`, оставь обе версии текста (удали строки `<<<<<<<`, `=======`, `>>>>>>>`), затем `git add CLAUDE.md` и `git stash drop`. Дальше — как выше.

---

## 2. На ПК: бэкап базы бота и остановка бота

Останови бота на ПК (Ctrl+C в окне, где он запущен). Telegram отдаёт сообщения только одному экземпляру — бот не должен работать одновременно на ПК и сервере.

```powershell
uv run python -m digest.admin --db data/link2note.db backup link2note-from-pc.db.gz
```
Файл `link2note-from-pc.db.gz` появится в папке проекта — его перенесём на сервер в шаге 4.

---

## 3. На сервере: установка

```bash
cd ~
git clone https://github.com/dtenditnyi/Link2Note.git link2note
cd link2note
```

### 3.1 Deploy key для хранилища (режим git — бот пока пишет в myObsidian сам)
```bash
mkdir -p secrets
ssh-keygen -t ed25519 -N '' -f secrets/deploy_key -C link2note
cat secrets/deploy_key.pub
```
Скопируй выведенную строку → GitHub → `Lol4ik0/myObsidian` → Settings → Deploy keys → **Add deploy key** → вставить, ✅ **Allow write access** → Add.

```bash
GIT_SSH_COMMAND="ssh -i secrets/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:Lol4ik0/myObsidian.git vault
sudo chown -R 1000:1000 vault secrets
chmod 600 secrets/deploy_key
```

### 3.2 `.env`
```bash
nano .env
```
Вставь (подставь свои значения):
```ini
BOT_TOKEN=<токен бота от @BotFather>
OWNER_ID=<твой Telegram ID>
GEMINI_API_KEY=<ключ нового проекта AI Studio для бота>

VAULT_BACKEND=git
VAULT_REPO=/vault
GIT_SSH_COMMAND=ssh -i /secrets/deploy_key -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new

TZ=Europe/Bucharest
```
Сохранить: Ctrl+O, Enter, Ctrl+X.

(Необязательно, для Instagram: положи cookies.txt со второго аккаунта в `secrets/instagram_cookies.txt` и добавь строку `COOKIES_FILE=/secrets/instagram_cookies.txt`, затем `sudo chown 1000:1000 secrets/instagram_cookies.txt`.)

### 3.3 Установка
```bash
chmod +x deploy/*.sh
./deploy/install.sh
```
Если скрипт сказал, что добавил тебя в группу `docker` и остановился — **выйди из SSH, зайди снова** и запусти `./deploy/install.sh` ещё раз.

Проверка:
```bash
./deploy/status.sh
docker compose logs --tail 50 bot
systemctl list-timers link2note-backup.timer
```
Ожидается: контейнер `running`, `healthy`, режим `git`, таймер бэкапа на 03:45.

---

## 4. Перенос базы с ПК на сервер

На ПК (замени `user@server` на свой логин и IP сервера):
```powershell
scp link2note-from-pc.db.gz user@server:~/link2note/backups/
```
На сервере:
```bash
cd ~/link2note
./deploy/restore.sh backups/link2note-from-pc.db.gz      # на вопрос ответить: yes
./deploy/status.sh
```
Напоминания, дубликаты, кнопки под конспектами и очередь переедут вместе с базой.

---

## 5. Проверка в Telegram (бот на сервере, режим git)

1. `/help` — бот отвечает.
2. Ссылка на короткое YouTube-видео → «⏳…» → конспект, кнопки. Через минуту заметка в Obsidian (`03 - Resources/📥 Из ссылок`).
3. Нажать «✅ Прочитано» → в заметке `status: read`.
4. «🔁 Подробнее» → заметка дополнилась (модель `gemini-3.8-flash`).
5. Голосовое «напомни через 2 минуты проверить бота» → напоминание пришло.
6. `/list` — старые напоминания с ПК на месте.

Если что-то не так — `docker compose logs --tail 100 bot` и чек-лист в `docs/DEPLOY.md`, раздел 11.

---

## 6. ПОТОМ — когда у сайта Second Brain будет готов API

1. Передать сайту контракт: файлы `docs/SECOND_BRAIN_API_CONTRACT.md` и папку `contract/` из этого репозитория (это сделает Claude Code сайта по промпту).
2. Добавить тег `fun` в заметку правил тегов в Obsidian («Теги (The tags)»).
3. На сайте в Настройках создать токен для бота: права `write` — `03 - Resources/📥 Из ссылок`, «Входящие», заметка задач; `delete` — только `03 - Resources/📥 Из ссылок`; право `ai`. Скопировать токен (показывается один раз).
4. Проверить, что бот видит сайт по внутренней сети:
   ```bash
   docker ps --format '{{.Names}}\t{{.Networks}}'      # имя контейнера сайта и его сеть
   ```
   Если имя не `secondbrain-web` — добавь в `.env` бота `SECOND_BRAIN_CONTAINER=<имя>`; если сайт не в сети `secondbrain`:
   ```bash
   docker network connect secondbrain <имя-контейнера-сайта>
   ```
5. Живая проверка контракта (токен вводится скрыто, в историю не попадёт):
   ```bash
   cd ~/link2note
   read -rs SECOND_BRAIN_API_TOKEN && export SECOND_BRAIN_API_TOKEN
   docker compose exec -e SECOND_BRAIN_API_TOKEN bot /app/.venv/bin/python scripts/check_second_brain_api.py
   ```
   Нужно, чтобы все строки были ✅.
6. Переключить бота на API:
   ```bash
   cd vault && git status && cd ..          # должно быть чисто (всё отправлено)
   nano .env                                # VAULT_BACKEND=api и строка SECOND_BRAIN_API_TOKEN=<токен>
   docker compose up -d
   docker compose exec bot /app/.venv/bin/python -m digest.admin api-health     # api: ok
   ./deploy/status.sh
   ```
7. Живой тест очереди: останови сайт (`docker stop <имя-контейнера-сайта>`), пришли боту ссылку → увидишь «📮 в очереди», `/queue` покажет её → запусти сайт (`docker start <имя>`) → через ≤1–5 мин сообщение сменится на обычное, в Obsidian **одна** заметка.
8. Проверить новые команды: `/task купить молоко завтра !`, `/note идея для проекта`, `/note неделя сделать лабу`, `/ask что я записывал про Proxmox?`.
9. Отметить в `docs/PLAN.md` проверенные вживую пункты этапа 5 и закоммитить.

---

## Обновления в будущем
```bash
cd ~/link2note && ./deploy/update.sh
```
Ручной бэкап: `./deploy/backup.sh`. Восстановление: `./deploy/restore.sh backups/<файл>.db.gz`.
