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

## Docker
```bash
mkdir -p data && sudo chown 1000:1000 data   # только на Linux-хосте
docker compose build && docker compose up -d
```
