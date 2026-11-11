# Этап 5 — цели и критерии приёмки

Задание: [PROMPT_SECOND_BRAIN_INTEGRATION.md](../PROMPT_SECOND_BRAIN_INTEGRATION.md). Тесты приёмки: `tests/acceptance/test_stage5.py` (id теста = `test_<цель>_…`).

| Цель | Критерии приёмки | Тесты |
|---|---|---|
| **G-0** main + автономия | `fix/ffmpeg-ytdlp` слит в `main` (`--no-ff`) и запушен; `gemini-resilience` разобран (DECISIONS D1), полезное перенесено с тестами; правила автономной работы — см. DECISIONS D2 (коммит правил заблокирован, ждёт владельца) | `test_g0_*` |
| **G-A1** интерфейс | `digest/vault_base.py`: Protocol `VaultBackend` (`ready`, `save`, `set_status`, `replace`, `delete`, `unread`, `find_by_url`); ошибки `VaultUnavailable` (retry_after), `VaultRejected`, `VaultQueued` — подклассы `VaultError` | `test_ga1_*` |
| **G-A2** GitVault | `GitVault` реализует `VaultBackend`; `Vault` — тот же класс (старые тесты зелёные без правок); `find_by_url` → `None` (дубликаты по локальной базе, как раньше) | `test_ga2_*`, `tests/test_vault*.py` |
| **G-A3** выбор backend | `VAULT_BACKEND=git|api` в `config.py`; `api` без URL/токена — ошибка конфигурации; `main.build_vault` возвращает `GitVault` / `QueuedVault`; хендлеры, worker, weekly, cli типизированы `VaultBackend` | `test_ga3_*` |
| **G-B1** контракт | `docs/SECOND_BRAIN_API_CONTRACT.md`: все эндпоинты из раздела 7 с примерами, коды ошибок, идемпотентность, раздел «Требования к сайту» | `test_gb1_*` |
| **G-B2** normalize | `contract/url_normalize_cases.json` ≥ 25 пар (YouTube, Reels `?dlrf=`/`igsh`, TikTok, utm/fbclid, якоря, слэши); бот проходит все | `test_gb2_*` |
| **G-B3** теги | `contract/allowed_tags.json` == `ALLOWED_TAGS` (включая `fun`); решение про `fun` в DECISIONS | `test_gb3_*` |
| **G-B4** фейковый сервер | `tests/fakes/second_brain.py`: aiohttp на localhost, весь контракт, токен и права по папкам, `Idempotency-Key` (повтор → тот же ответ, другое тело → 409), инъекция сбоев: выключен, 500, 503+Retry-After, 429, таймаут, 409, 403, 422, обрыв после записи | `test_gb4_*`, `tests/test_fake_second_brain.py` |
| **G-C1** ApiVault | `digest/vault_api.py` на httpx: URL/токен из настроек, таймаут, `Idempotency-Key` у каждой записи, ошибки → `VaultUnavailable`/`VaultRejected`/`NoteNotFound`/`NoteAmbiguous`; `ready` = `GET /health`; токен не попадает в логи и `repr` | `test_gc1_*`, `tests/test_vault_api.py` |
| **G-C2** outbox-таблица | `vault_ops`: op, grp, payload JSON (с готовым Markdown), уникальный `idem_key`, chat/msg, status pending/done/dead, attempts, next_attempt_at (UTC), last_error, created/updated; миграция идемпотентна | `test_gc2_*` |
| **G-C3** поведение очереди | запись сначала в outbox, потом отправка; недоступен → pending + бэкофф 30 с→1→5→15 мин→1 ч (с `Retry-After`) и «📮 Сайт недоступен…», потом сообщение обновляется; 403/422 → dead без повторов + сообщение с причиной и `/retry`; порядок операций одной заметки; переживает перезапуск, размер очереди в логе; старше `OUTBOX_MAX_AGE` / больше `OUTBOX_MAX_ATTEMPTS` → dead + уведомление; `/queue`, `/retry`; дубликаты через `lookup`, при недоступности — локальная `digests` | `test_gc3_*`, `tests/test_outbox.py` |
| **G-C4** сценарии сбоев | выключен→включён (ровно одна заметка); обрыв после записи (нет дубля); 500×3 → успех; 429+Retry-After; 403/422 → dead; перезапуск посреди очереди; порядок; «Удалить» до создания; 5 ссылок сразу; большая заметка; кириллица+эмодзи; aware UTC; стресс со случайными сбоями | `test_gc4_*`, `tests/test_outbox_scenarios.py` |
| **G-D1** `/task` | `POST /tasks` через outbox; «завтра», «до пятницы» → due; «!» → priority high; `#тег` — только разрешённые | `test_gd1_*` |
| **G-D2** `/note` | `POST /capture` target inbox; `/note неделя …` → weekly; через outbox | `test_gd2_*` |
| **G-D3** `/ask` | `POST /ask` напрямую; источники в ответе; Markdown → безопасный HTML; длинный ответ режется ≤ 4096; недоступен → «сайт недоступен, попробуй позже» | `test_gd3_*` |
| **G-D4** голос | DECISIONS D11: расшифровка «добавь задачу …» → как `/task`, «запиши в заметки …» → как `/note`, без Gemini; остальное — напоминания, как раньше | `test_gd4_*` |
| **G-D5** `/queue` `/retry` `/help` | команды работают, `/help` и меню команд обновлены; в режиме git новые команды отвечают «нужен VAULT_BACKEND=api» | `test_gd5_*` |
| **G-E1** модель «Подробнее» | `GEMINI_DETAIL_MODEL` (по умолчанию `gemini-3.8-flash`) только для «🔁 Подробнее», при перегрузке → основная; пусто — выключено; PLAN.md обновлён | `test_ge1_*` |
| **G-F1** compose | внешняя сеть `${DOCKER_NETWORK:-secondbrain}`, `SECOND_BRAIN_API_URL` по умолчанию на контейнер сайта, ротация логов, `TZ`, `restart` | `test_gf1_*` |
| **G-F2** healthcheck | heartbeat раз в 30 с из event loop; `HEALTHCHECK` проверяет свежесть < 120 с | `test_gf2_*`, `tests/test_heartbeat.py` |
| **G-F3** скрипты | `deploy/install.sh`, `update.sh`, `backup.sh`, `restore.sh`, `status.sh` (`set -euo pipefail`, лог в `deploy/logs/`), `link2note.service`, `link2note-backup.timer` (03:45); `python -m digest.admin queue|backup` | `test_gf3_*`, `tests/test_admin.py` |
| **G-F4** DEPLOY.md | пошаговая инструкция, все переменные `config.py`, ссылки в README и CLAUDE.md | `test_gf4_*` |
| **G-F5** проверка деплоя | Docker: образ, `-p link2note-test`, `DRY_RUN`, healthcheck, `docker kill` → рестарт, backup/restore; иначе — `compose config`, `bash -n`, честно в отчёте | `test_gf5_*`, REPORT.md |
| **G-G1** живая проверка | `scripts/check_second_brain_api.py`: URL/токен из окружения, все эндпоинты, идемпотентность, ✅/❌; прогнан против фейкового сервера в тесте | `test_gg1_*` |
| **G-E2E** | ссылка → outbox → сайт выключен → включён → ровно одна заметка → Прочитано → Подробнее → Удалить → `/task` → `/note` → `/ask` → `/unread` → состояние совпадает; то же с перезапуском посередине | `test_e2e_*` |
| **G-final** | нет `xfail` в acceptance, `pytest -q` и ruff зелёные, финальная проверка, REPORT, пуш | — |
