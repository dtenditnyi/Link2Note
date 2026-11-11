# Этап 5 — журнал

### 2026-11-11 — G-0 — main и подготовка
Закоммичен фикс ffmpeg (630 passed, ruff ok) → `--no-ff` в `main`, пуш (`ec996ac`, `02602a2`). `gemini-resilience` частично перекрыт `main` — переносится только голосовое при перегрузке Gemini (D1). Правки правил автономии заблокированы классификатором Claude Code → `git stash` на `main` (D2). Ветка `feature/second-brain-integration` (`e7fed59`).

### 2026-11-11 — GOALS + acceptance
GOALS.md (26 целей), DECISIONS D1–D12, `tests/acceptance/test_stage5.py`: 3 passed / 30 xfailed. ruff: isort знает свои пакеты (`known-first-party`).

### 2026-11-11 — G-A1/G-A2 — интерфейс хранилища
`digest/vault_base.py` (Protocol `VaultBackend`, `UnreadNote`), ошибки `VaultUnavailable`/`VaultRejected`/`VaultQueued`; `Vault` → `GitVault` + алиас, `ready()`, `find_by_url()→None`. Варианты → D3. Старые тесты без правок: 98 passed. Коммит `8f11b57`.

### 2026-11-11 — G-B1…B4 — контракт и фейковый сервер
`docs/SECOND_BRAIN_API_CONTRACT.md`, `contract/url_normalize_cases.json` (38 пар), `contract/allowed_tags.json` (17 тегов, `fun` — D9), `tests/fakes/second_brain.py` (aiohttp, все эндпоинты, права, идемпотентность, сбои 500/503/429/401/403/409/422/timeout/drop/выключен). Настройки этапа 5 в `config.py` (+12 тестов). Тесты: 676 passed / 24 xfailed. Найден B01 (тест ходил в Telegram).

### 2026-11-11 — G-A1/G-A2 — интерфейс хранилища
`digest/vault_base.py` (Protocol `VaultBackend`, `UnreadNote`), ошибки `VaultUnavailable`/`VaultRejected`/`VaultQueued`; `Vault` → `GitVault` + алиас, `ready()`, `find_by_url()→None`. Варианты → D3. Старые тесты без правок: 98 passed. Коммит `8f11b57`.

### 2026-11-11 — G-B1…B4 — контракт и фейковый сервер
`docs/SECOND_BRAIN_API_CONTRACT.md`, `contract/url_normalize_cases.json` (38 пар), `contract/allowed_tags.json` (17 тегов, `fun` — D9), `tests/fakes/second_brain.py` (aiohttp, все эндпоинты, права, идемпотентность, сбои 500/503/429/401/403/409/422/timeout/drop/выключен). Настройки этапа 5 в `config.py` (+12 тестов). Тесты: 676 passed / 24 xfailed. Найден B01 (тест ходил в Telegram). Коммит `244b4ab`.

### 2026-11-11 — G-C1…C4 — ApiVault и офлайн-очередь
`digest/vault_api.py` (httpx, ключи идемпотентности, uuid5 для if_sha — D6, 403 вне папки — D13), таблица `vault_ops`, `digest/outbox.py` (`Outbox` + `QueuedVault`, группы по url — D4/D5, бэкофф 30 с→1 ч, Retry-After, dead + уведомление, max age/attempts, /retry), воркер и кнопки понимают `VaultQueued`, дубликаты через lookup (D7). Тесты: vault_api 32, outbox 21, сценарии+стресс 23. Баг B03.

### 2026-11-11 — G-A3, G-D1…D5, G-E1, G-F2 — выбор backend, команды сайта, модель «Подробнее», heartbeat
`main.build_vault`/`build_llms`, `DRY_RUN`; `handlers/brain.py` (/task /note /ask /queue /retry), `services/tasks.py`, `services/tg_format.py`, голос «добавь задачу/запиши в заметки» (D11), `GEMINI_DETAIL_MODEL`; `services/heartbeat.py` + `HEALTHCHECK`. Правки старых тестов — D14. Ревью A+B: 9 находок исправлено (REVIEW-AB.md, B02). Тесты: 820 passed / 6 xfailed.

### 2026-11-11 — G-0 — перенос из gemini-resilience
Голосовое при перегрузке Gemini (429/5xx после запасной): наш парсер времени → черновик «(разобрал без Gemini)»; не нашёл → «🎧 Я услышал…» + «🔁 Повторить» (`TextStore`, 6 ч, без повторного Whisper). Варианты → D1. Тесты: `tests/test_voice_outage.py` 5 passed.

### 2026-11-11 — G-F1…F5, G-G1 — сервер и живая проверка
compose (сеть `secondbrain`, ротация логов, URL сайта из имени контейнера), `deploy/*.sh`, systemd, `digest/admin.py`, `docs/DEPLOY.md`, `scripts/check_second_brain_api.py` (15 проверок, прогнан против фейка + негативный тест). F5 вживую в Docker Desktop — `F5-docker-check.md`; найден и исправлен B04 (SIGTERM). Полный набор: 836 passed.

### 2026-11-11 — ревью C/D/E — 8 находок исправлено
B05 (HIGH, чужая заметка при « (2)»), B06 (`/ask` HTML), даты `/task`, `/retry` и порядок, max_attempts для ошибок бота, дубликат при ждущем удалении, `aclose`. Закрыты выжившие мутации (g, h, c2, b). Отчёт — REVIEW-CDE.md.
