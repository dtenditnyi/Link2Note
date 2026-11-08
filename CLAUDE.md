# Link2Note

Telegram-бот «Посмотрю потом»: я кидаю ссылку (YouTube, Reels, TikTok, статья) → Gemini делает конспект → Markdown-заметка → `git push` в моё Obsidian-хранилище.
Полный план, архитектура, этапы и черновики кода: @docs/PLAN.md

## Стек
Python 3.12, uv, aiogram 3, APScheduler, SQLite, google-genai, yt-dlp, trafilatura, httpx, Jinja2, pydantic / pydantic-settings, faster-whisper (этап 3). Деплой: Docker Compose на домашнем сервере.

## Команды
- Установить зависимости: `uv sync`
- Тесты: `uv run pytest -q tests/<файл>` (по одному файлу, весь набор — только перед коммитом)
- Линт и формат: `uv run ruff check --fix . && uv run ruff format .`
- Конспект без Telegram: `uv run python -m digest.cli <url>` (появится на этапе 1)
- Docker: `docker compose build && docker compose up -d`

## Правила
- Работаем по этапам из docs/PLAN.md. Не делать функционал следующих этапов без запроса.
- Весь код конспектов — в `digest/`, Telegram-хендлеры — в `handlers/`.
- Секреты только через `.env` → `config.py` (pydantic-settings). Никогда не коммитить ключи, cookies, `secrets/`, `vault/`.
- Бот отвечает только `OWNER_ID` — этот фильтр не убирать.
- Сетевые вызовы (Gemini, yt-dlp, httpx, git) в тестах мокать. Живой прогон — только вручную.
- Теги заметок — только из `ALLOWED_TAGS` в `digest/llm.py`, максимум 3.
- API aiogram 3 и google-genai меняются: перед использованием сверяйся с документацией (Context7), не по памяти.
- Маленькие коммиты, сообщения на английском. Показывай вывод тестов как доказательство.

## При компакции сохранять
Текущий этап из PLAN.md, список изменённых файлов, команды тестов, нерешённые вопросы.
