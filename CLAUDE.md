# Link2Note

Telegram-бот «Посмотрю потом»: я кидаю ссылку (YouTube, Reels, TikTok, статья) → Gemini делает конспект → Markdown-заметка → `git push` в моё Obsidian-хранилище.
Полный план, архитектура, этапы и черновики кода: @docs/PLAN.md
Старый бот-напоминалка (assistant_bot) перенесён на этапе 0 — что и почему поменялось: docs/MIGRATION.md, docs/specs/stage-0-migration.md.

## Как работать
**Любую функцию, переделку или перенос делай строго по @docs/WORKFLOW.md:** задача → исследование (код, Context7, интернет) → план с критериями приёмки и моё «ок» → сначала тесты (красные) → код → все тесты + ruff + ревью субагентом → коммит после подтверждения.

## Стек
Python 3.12, uv, aiogram 3, APScheduler, SQLite, google-genai, yt-dlp, trafilatura, httpx, Jinja2, pydantic / pydantic-settings, faster-whisper (голосовые, модель `small` на CPU). Деплой: Docker Compose на домашнем сервере.

## Команды
- Установить зависимости: `uv sync`
- Тесты: `uv run pytest -q tests/<файл>` (по одному файлу, весь набор — только перед коммитом)
- Линт и формат: `uv run ruff check --fix . && uv run ruff format .`
- Запустить бота локально: `uv run python main.py` (нужен заполненный `.env`; БД — `data/link2note.db`)
- Конспект без Telegram: `uv run python -m digest.cli <url> [--hint "…"] [--save]` — любой источник (YouTube, Reels, TikTok, X, статья); живой вызов Gemini (и запасной путь через Whisper), дубликаты не проверяет; `--save` — commit+push в `VAULT_REPO`
- Типы: `~/.local/bin/pyright.exe --pythonpath .venv/Scripts/python.exe <файлы>` (до этапа 1 в старом коде ~38 ошибок — смотри только свои файлы)
- Ручная проверка vault — только `../vault-test` (`remote.git` + `clone`), настоящий myObsidian не трогать
- Docker: `docker compose build && docker compose up -d`

## Правила
- Часовой пояс Europe/Bucharest, бот на русском.
- Работаем по этапам из docs/PLAN.md. Не делать функционал следующих этапов без запроса.
- Весь код конспектов — в `digest/`, логика без Telegram (время, LLM-разбор, Whisper) — в `services/`, Telegram-хендлеры — в `handlers/`, клавиатуры и callback-данные — в `handlers/keyboards.py`.
- Роутеры подключаются в `main.build_dispatcher` в порядке basic → reminders → voice → links → notes → context; `context` — всегда последним.
- Кнопки под конспектом (`NoteCb`, таблица `notes`): правда о «прочитано» — frontmatter файла, не БД. Заметки меняет только `Vault` (`set_status`/`replace`/`delete`, под `Vault.lock`); заметку ищем по имени, иначе по `url:` во frontmatter.
- Whisper-путь конспекта — только при `VideoUnreadable` (Gemini не открыл видео), никогда при 429/503/дневной квоте: конспект по расшифровке тоже тратит запрос Gemini.
- Схема ответа Gemini (`digest/schema.py`): необязательные поля — только `Field(default_factory=…)`, не `= ""`: литеральный `default` уходит в запрос к Gemini (тест L4 это ловит).
- Время в БД — UTC; всё время в коде — aware datetime; часовой пояс только из `config.py`.
- Секреты только через `.env` → `config.py` (pydantic-settings). Никогда не коммитить ключи, cookies, `secrets/`, `vault/`.
- Бот отвечает только `OWNER_ID` — этот фильтр не убирать.
- Сетевые вызовы (Gemini, yt-dlp, httpx, git) в тестах мокать. Живой прогон — только вручную.
- Теги заметок — только из `TAG_MEANINGS`/`ALLOWED_TAGS` в `digest/llm.py`, максимум 3; новый тег — сразу с описанием смысла.
- yt-dlp синхронный — только через `asyncio.to_thread`; `cookiefile` — всегда копия (yt-dlp перезаписывает файл при закрытии). Обновлять yt-dlp: `uv lock --upgrade-package yt-dlp`.
- Gemini: основная `GEMINI_MODEL=gemini-3.5-flash-lite`, запасная `GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite` (пусто — выключено); на free tier у Lite 500 запросов/сутки и 15/мин, у `gemini-3.8-flash` — всего 20/сутки. Запасная — одна попытка при 429/5xx и в конспектах, и в напоминаниях; «перегружен» определяет только `services/gemini_errors.py`. Конспект при перегрузке обеих моделей откладывается (`GeminiBusy` → `Postponer`), не падает. Список моделей с ключом: `client.models.list()`.
- API aiogram 3 и google-genai меняются: перед использованием сверяйся с документацией (Context7), не по памяти.
- Маленькие коммиты, сообщения на английском. Показывай вывод тестов как доказательство.

## При компакции сохранять
Текущий этап из PLAN.md, список изменённых файлов, команды тестов, нерешённые вопросы.
