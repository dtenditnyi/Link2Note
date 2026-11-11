# Контракт API Second Brain v1 (для бота Link2Note)

Документ для репозитория сайта Second Brain. Бот Link2Note реализован и проверен по этому контракту на фейковом сервере (`tests/fakes/second_brain.py` — эталонное поведение, его можно читать как исполняемую спецификацию). Живая проверка сайта — `scripts/check_second_brain_api.py`.

Общие файлы контракта (копировать в репозиторий сайта и прогонять в его тестах):
- `contract/url_normalize_cases.json` — пары «ссылка → ключ» для поиска заметки по url;
- `contract/allowed_tags.json` — теги заметок-конспектов бота (включая `fun`).

---

## 1. Общие правила

**База:** `/api/v1`. Бот ходит напрямую по внутренней docker-сети: `http://secondbrain-web:8000/api/v1` (не через интернет и Cloudflare Access).

**Авторизация:** `Authorization: Bearer <token>`. Права токена:
| Право | Что даёт |
|---|---|
| `read` | `GET` заметок, задач, `lookup`, `health` |
| `write` | `POST/PATCH/PUT` в папках из `write_paths` |
| `delete` | `DELETE` в папках из `delete_paths` |
| `ai` | `POST /ask` |

Токен бота: `write_paths = ["03 - Resources/📥 Из ссылок", "<папка «Входящие»>", "<заметка задач>"]`, `delete_paths = ["03 - Resources/📥 Из ссылок"]`. Папка «в правах» — сама папка и все вложенные. `GET /health` можно без токена.

**Пути заметок** — относительно корня хранилища, с расширением `.md`, разделитель `/`. В URL каждый сегмент percent-encoded в UTF-8, `/` между сегментами — как есть:
`03 - Resources/📥 Из ссылок/2026-11-11 Хоткеи.md` → `/notes/03%20-%20Resources/%F0%9F%93%A5%20%D0%98%D0%B7%20%D1%81%D1%81%D1%8B%D0%BB%D0%BE%D0%BA/2026-11-11%20%D0%A5%D0%BE%D1%82%D0%BA%D0%B5%D0%B8.md`.
Пути с `..`, `\`, `:`, абсолютные, с сегментом `.git` или `.obsidian` на любом уровне — 400 `validation_error`. Папка «в правах» сравнивается по целым сегментам: `…/📥 Из ссылок2` **не** входит в `…/📥 Из ссылок`.

**Запись:** сайт сам делает `git pull` → запись → `commit` → `push`. Ответ на изменяющий запрос приходит **после успешного commit**; если push не удался, он уходит в очередь сайта, а в ответе `"pushed": false`.

**Идемпотентность.** Все изменяющие запросы (`POST`, `PATCH`, `PUT`, `DELETE`) принимают заголовок `Idempotency-Key: <UUID>`; бот шлёт его всегда.
- Тот же ключ + тот же запрос → тот же код и то же тело ответа, повторной записи нет. Сайт хранит ответы ≥ 7 дней.
- «Тот же запрос» сравнивается **по смыслу, а не по байтам**: метод + раскодированный путь + отсортированные параметры query + разобранный JSON (порядок ключей и экранирование не важны).
- Тот же ключ + другой запрос → `409 idempotency_mismatch`.
- **Кэшируются только окончательные ответы** (2xx и 4xx, кроме `409 in_progress` и `429`). Ответы 5xx, `429` и сетевые сбои **не** запоминаются: повтор с тем же ключом выполняется заново, иначе временная ошибка стала бы вечной.
- Ключи — в пространстве токена (у разных токенов независимы).
- Повтор, пришедший, пока первый запрос с этим ключом ещё выполняется, — `409 in_progress` (`retryable: true`) или ожидание результата первого.

**Время** — ISO 8601 с часовым поясом (`2026-11-11T10:00:00Z`); даты задач — `YYYY-MM-DD`.

**Ошибки** — всегда JSON:
```json
{"error": {"code": "forbidden", "message": "Папка 01 - Projects вне прав токена", "retryable": false}}
```
| HTTP | `code` | Повторять? | Как реагирует бот |
|---|---|---|---|
| 400, 422 | `validation_error` | нет | операция `dead`, сообщение владельцу с причиной |
| 401 | `unauthorized` | нет | `dead` (неверный/отозванный токен) |
| 403 | `forbidden` | нет | `dead` (папка вне `write_paths`/`delete_paths` или нет права `ai`) |
| 404 | `not_found` | нет | заметки нет (для удаления — «уже удалена», это успех) |
| 409 | `conflict` | да, после перечитывания | `if_sha` не совпал; в теле `"current_sha": "…"` |
| 409 | `in_progress` | да | запрос с этим `Idempotency-Key` ещё выполняется |
| 409 | `idempotency_mismatch` | нет | `dead` (ошибка бота) |
| 429 | `rate_limited` | да | ждать `Retry-After` (секунды) |
| 500, 502, 504 | `internal_error` | да | повтор с нарастающей паузой |
| 503 | `vault_unavailable` | да | ждать `Retry-After`; хранилище занято/синхронизируется |

Сетевые ошибки и таймауты бот повторяет так же, как 5xx (30 с → 1 мин → 5 мин → 15 мин → 1 ч → далее раз в час, не раньше `Retry-After`).

---

## 2. Эндпоинты

| Метод и путь | Назначение |
|---|---|
| `GET /health` | доступность и синхронизация |
| `POST /notes` | создать заметку из готового Markdown |
| `GET /notes/lookup?url=` | найти заметки по ссылке |
| `GET /notes/{path}` | прочитать заметку |
| `GET /notes?folder=&fm.<ключ>=<значение>&sort=&limit=` | список с фильтром по frontmatter |
| `PATCH /notes/{path}/frontmatter` | изменить поля frontmatter |
| `PUT /notes/{path}` | заменить содержимое |
| `DELETE /notes/{path}?if_sha=` | удалить (остаётся в истории git) |
| `POST /capture` | быстрый захват во «Входящие» или недельную заметку |
| `GET /tasks?status=&due_before=` | список задач |
| `POST /tasks` | создать задачу |
| `PATCH /tasks/{id}` | изменить задачу |
| `DELETE /tasks/{id}` | удалить задачу |
| `POST /ask` | вопрос ИИ по заметкам |

### GET /health
Без авторизации.
```http
GET /api/v1/health
```
```json
200 {"status": "ok", "vault_synced_at": "2026-11-11T10:00:00Z", "version": "1.4.0"}
```
`status`: `ok` | `degraded` (push в очереди) — бот считает сайт доступным при `200`. Хранилище не готово → `503 vault_unavailable`.

### POST /notes — создать заметку
```http
POST /api/v1/notes
Authorization: Bearer <token>
Idempotency-Key: 6f1c2e5a-0d4e-4b7a-9a43-3f1d2b0c9e11
Content-Type: application/json

{
  "folder": "03 - Resources/📥 Из ссылок",
  "name": "2026-11-11 10 горячих клавиш VS Code",
  "content": "---\ntype: digest\nstatus: unread\nsource: youtube\nurl: https://www.youtube.com/watch?v=dQw4w9WgXcQ\nauthor: Fireship\ndate_added: 2026-11-11\nread_minutes: 3\nconfidence: high\nmodel: gemini-3.5-flash-lite\ntags: [dev]\n---\n# ⌨️ 10 горячих клавиш VS Code\n…",
  "source": {"url": "https://youtu.be/dQw4w9WgXcQ?si=x", "kind": "youtube", "author": "Fireship", "duration": null, "model": "gemini-3.5-flash-lite"}
}
```
```json
201 {"path": "03 - Resources/📥 Из ссылок/2026-11-11 10 горячих клавиш VS Code.md",
     "name": "2026-11-11 10 горячих клавиш VS Code", "sha": "9b2f…", "commit": "a41c9e0", "pushed": true}
```
- `name` — желаемое имя без `.md`: `YYYY-MM-DD ` + заголовок (бот уже убрал `\ / : * ? " < > | # ^ [ ]`, заголовок ≤ 80 символов, всё имя ≤ 91). Сайт принимает имена до 120 символов. Имя занято → сайт добавляет « (2)», « (3)»… и возвращает итоговое `name`.
- `content` — полный Markdown с frontmatter, сайт пишет его **как есть** (байт в байт, LF). Проверки: frontmatter есть; для `type: digest` — `url` задан, `status` ∈ {`unread`, `read`}, `tags` ⊆ `contract/allowed_tags.json`, не больше 3 → иначе `422 validation_error`.
- `folder` вне `write_paths` → `403`.
- `sha` — версия файла (любой стабильный хеш содержимого, например git blob sha); используется в `if_sha`.

### GET /notes/lookup?url= — найти по ссылке
```http
GET /api/v1/notes/lookup?url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ%3Fsi%3Dx
```
```json
200 {"items": [{"path": "03 - Resources/📥 Из ссылок/2026-11-11 10 горячих клавиш VS Code.md",
                "name": "2026-11-11 10 горячих клавиш VS Code",
                "frontmatter": {"type": "digest", "status": "unread", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}}]}
```
Сайт нормализует и `url` из запроса, и `url:` из frontmatter **всех** заметок хранилища (заметку могли переместить) по правилам `contract/url_normalize_cases.json` и возвращает совпавшие (0, 1 или несколько). Ничего не найдено — `200 {"items": []}`, не 404.

### GET /notes/{path}
```json
200 {"path": "…/2026-11-11 Хоткеи.md", "name": "2026-11-11 Хоткеи",
     "content": "---\n…", "frontmatter": {"type": "digest", "status": "unread", …}, "sha": "9b2f…"}
```
Нет файла → `404 not_found`.

### GET /notes — список с фильтром
```http
GET /api/v1/notes?folder=03%20-%20Resources%2F%F0%9F%93%A5%20%D0%98%D0%B7%20%D1%81%D1%81%D1%8B%D0%BB%D0%BE%D0%BA&fm.type=digest&fm.status=unread&sort=fm.date_added&limit=200
```
```json
200 {"items": [{"path": "…", "name": "2026-11-01 Старая", "title": "📚 Старая",
                "frontmatter": {"type": "digest", "status": "unread", "date_added": "2026-11-01", "read_minutes": "3"}}]}
```
- `folder` — с вложенными папками; `fm.<ключ>=<значение>` — точное совпадение строки (несколько — И);
- `sort` — `fm.<ключ>` по возрастанию, `-fm.<ключ>` по убыванию; `limit` по умолчанию 100, максимум 500;
- `title` — первый заголовок `# …` (или имя файла).
Значения frontmatter отдаются строками (как в файле, без кавычек YAML); списки (`tags`) — массивами.

### PATCH /notes/{path}/frontmatter
```http
PATCH /api/v1/notes/03%20-%20Resources/…/2026-11-11%20Хоткеи.md/frontmatter
Idempotency-Key: …
{"set": {"status": "read"}, "if_sha": "9b2f…"}
```
```json
200 {"path": "…", "sha": "c07d…", "commit": "b1e2f3a", "pushed": true}
```
Меняются **только** указанные строки frontmatter (отсутствующий ключ добавляется в конец frontmatter), остальной текст и форматирование — байт в байт. Результат проверяется так же, как при создании (для `type: digest` — `status`, `tags`), иначе `422`. `if_sha` необязателен; не совпал → `409 conflict` + `current_sha`. Ничего не изменилось (значение уже такое) — `200` без нового коммита (`commit` — последний коммит файла).

### PUT /notes/{path} — заменить содержимое
```json
{"content": "---\n…", "if_sha": "9b2f…"}
→ 200 {"path": "…", "sha": "e5f6…", "commit": "c3d4e5f", "pushed": true}
→ 409 {"error": {"code": "conflict", "message": "…", "retryable": true}, "current_sha": "7a8b…"}
```
Те же проверки содержимого, что у `POST /notes`. `if_sha` обязателен (защита от затирания правок, сделанных в Obsidian).

### DELETE /notes/{path}?if_sha=
`204` без тела. Только в `delete_paths` (иначе `403`). Файл исчезает из рабочей копии, но остаётся в истории git. Нет файла → `404`. `if_sha` необязателен; не совпал → `409 conflict`.

### POST /capture — быстрый захват
```json
{"text": "идея: бот для Anki", "target": "inbox", "source": {"kind": "telegram"}}
→ 201 {"path": "00 - Inbox/2026-11-11.md", "name": "2026-11-11", "commit": "d4e5f6a", "pushed": true}
```
`target`: `inbox` — во «Входящие» (сайт сам решает: отдельная заметка или дописать в заметку дня); `weekly` — дописать в текущую недельную заметку. Текст ≤ 10 000 символов, Markdown.

### Задачи
```json
GET /tasks?status=open&due_before=2026-11-15
→ 200 {"items": [{"id": "t_8f3a", "text": "сдать лабу", "status": "open", "due": "2026-11-13", "priority": "high", "tags": ["university"]}]}

POST /tasks  {"text": "сдать лабу", "due": "2026-11-13", "priority": "high", "tags": ["university"]}
→ 201 {"id": "t_8f3a", "text": "сдать лабу", "status": "open", "due": "2026-11-13", "priority": "high", "tags": ["university"], "path": "…/Задачи.md"}

PATCH /tasks/t_8f3a  {"status": "done"}        → 200 {"id": "t_8f3a", …}
DELETE /tasks/t_8f3a                            → 204
```
`priority`: `high` | `normal` | `low` (по умолчанию `normal`); `status`: `open` | `done`; `due` — `YYYY-MM-DD` или `null`; `tags` — из `allowed_tags.json` (иначе `422`).

### POST /ask — вопрос ИИ по заметкам
```json
{"question": "что я знаю про Docker?", "provider": null}
→ 200 {"answer_markdown": "**Docker** — …", "sources": [{"path": "…/2026-11-01 Docker.md", "name": "2026-11-01 Docker"}], "model": "…"}
```
Нужно право `ai`. Ответ может быть длинным — бот сам режет его под лимит Telegram. ИИ недоступен → `503`.

---

## 3. Как бот использует API

| Действие в Telegram | Запросы |
|---|---|
| ссылка → конспект | `GET /notes/lookup` (дубликат?) → `POST /notes` |
| ✅ Прочитано | `GET /notes/{path}` (если 404 — `lookup`) → `PATCH …/frontmatter {"set":{"status":"read"}}` (если уже `read` — запроса нет) |
| 🔁 Подробнее | `GET /notes/{path}` → `PUT /notes/{path}` с `if_sha` (если текст уже такой — запроса нет); `409` → перечитать и повторить |
| 🗑 Удалить | `GET /notes/{path}` → `DELETE ?if_sha=`; `404` = уже удалена |
| `/unread`, воскресный дайджест | `GET /notes?folder=…&fm.type=digest&fm.status=unread&sort=fm.date_added` |
| `/task` | `POST /tasks` |
| `/note`, `/note неделя` | `POST /capture` (`inbox` / `weekly`) |
| `/ask` | `POST /ask` |

Заметку, переименованную или перемещённую в Obsidian, бот ищет через `lookup` по url. Найдено несколько — бот ничего не меняет и пишет владельцу список.

Ключ идемпотентности: у `POST` — один UUID на операцию на всё время её жизни в очереди бота. У запросов с `if_sha` — `uuid5(ключ операции, sha)`, чтобы повтор после смены версии не давал `idempotency_mismatch`.

---

## 4. Требования к сайту

1. **Идемпотентность** всех изменяющих запросов по `Idempotency-Key` (≥ 7 дней, тот же ответ; другое тело → `409 idempotency_mismatch`). Без этого офлайн-очередь бота может создать дубли, когда ответ потерялся после записи.
2. **Права токена по папкам:** `write_paths`, `delete_paths`, право `ai`; ошибки `401`/`403` в формате контракта.
3. **Поиск по нормализованному url** (`GET /notes/lookup`) во всём хранилище; реализация проходит все пары `contract/url_normalize_cases.json`.
4. **Фильтр по frontmatter** в `GET /notes` (`folder`, `fm.*`, `sort`, `limit`, поле `title`).
5. **`PATCH` frontmatter** меняет только указанные ключи, остальной текст — байт в байт.
6. **`DELETE`** только в `delete_paths`, файл остаётся в истории git.
7. **Разрешённые теги** включают все теги `contract/allowed_tags.json`, в том числе `fun` (его нужно добавить в заметку правил тегов хранилища).
8. **Создание заметки из готового Markdown**: содержимое пишется как есть, проверяются frontmatter и теги, конфликт имени → « (2)».
9. **Формат ошибок** `{"error": {code, message, retryable}}`, `Retry-After` у `429`/`503`, `current_sha` у `409 conflict`.
10. **Ответ после commit**, push может быть отложен (`"pushed": false`); `GET /health` — без токена.
11. Логи сайта не пишут токены и полный текст заметок.
