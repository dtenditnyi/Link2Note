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
