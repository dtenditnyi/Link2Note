# Link2Note

Telegram-бот «Посмотрю потом»: ссылка на видео или статью → конспект через Gemini → заметка в Obsidian.
Плюс напоминания (текстом и голосом), перенесённые из assistant_bot.

План и архитектура: [docs/PLAN.md](docs/PLAN.md)

## Запуск локально
Создай `.env` со списком переменных из [docs/PLAN.md, раздел 6.8](docs/PLAN.md) (минимум `BOT_TOKEN`, `OWNER_ID`, `GEMINI_API_KEY`).
```powershell
uv sync
uv run pytest -q
uv run python main.py
```

## Сервер (Docker)
Пошагово — [docs/DEPLOY.md](docs/DEPLOY.md): `.env`, `./deploy/install.sh`, обновление, бэкапы, переключение на API сайта Second Brain.
```bash
./deploy/install.sh     # первая установка (Docker, папки, сеть, systemd)
./deploy/status.sh      # состояние, очередь сайта, бэкапы
./deploy/update.sh      # обновление с откатом
```

## Second Brain
Режим `VAULT_BACKEND=api`: заметки пишет сайт Second Brain, бот шлёт готовый Markdown через офлайн-очередь.
Контракт API для сайта — [docs/SECOND_BRAIN_API_CONTRACT.md](docs/SECOND_BRAIN_API_CONTRACT.md), общие файлы — `contract/`.
Команды: `/task`, `/note`, `/ask`, `/queue`, `/retry`.
