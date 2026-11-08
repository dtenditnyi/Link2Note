# Link2Note

Telegram-бот «Посмотрю потом»: ссылка на видео или статью → конспект через Gemini → заметка в Obsidian.

План и архитектура: [docs/PLAN.md](docs/PLAN.md)

## Запуск локально
```powershell
uv sync
copy .env.example .env   # заполнить ключи
uv run pytest -q
```
