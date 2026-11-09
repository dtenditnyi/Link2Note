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
- Конспект без Telegram: `uv run python -m digest.cli <url>` (появится на этапе 1)
- Docker: `docker compose build && docker compose up -d`

## Правила
- Часовой пояс Europe/Bucharest, бот на русском.
- Работаем по этапам из docs/PLAN.md. Не делать функционал следующих этапов без запроса.
- Весь код конспектов — в `digest/`, логика без Telegram (время, LLM-разбор, Whisper) — в `services/`, Telegram-хендлеры — в `handlers/`, клавиатуры и callback-данные — в `handlers/keyboards.py`.
- Роутеры подключаются в `main.build_dispatcher` в порядке basic → reminders → voice → (links) → context; `context` — всегда последним.
- Время в БД — UTC; всё время в коде — aware datetime; часовой пояс только из `config.py`.
- Секреты только через `.env` → `config.py` (pydantic-settings). Никогда не коммитить ключи, cookies, `secrets/`, `vault/`.
- Бот отвечает только `OWNER_ID` — этот фильтр не убирать.
- Сетевые вызовы (Gemini, yt-dlp, httpx, git) в тестах мокать. Живой прогон — только вручную.
- Теги заметок — только из `ALLOWED_TAGS` в `digest/llm.py`, максимум 3.
- API aiogram 3 и google-genai меняются: перед использованием сверяйся с документацией (Context7), не по памяти.
- Маленькие коммиты, сообщения на английском. Показывай вывод тестов как доказательство.

## При компакции сохранять
Текущий этап из PLAN.md, список изменённых файлов, команды тестов, нерешённые вопросы.
