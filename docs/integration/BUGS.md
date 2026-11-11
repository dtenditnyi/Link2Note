# Этап 5 — найденные баги

Формат: симптом → причина → исправление → регрессионный тест.

### B01 — xfail-тест приёмки ходил в настоящий Telegram
- Симптом: `ResourceWarning: unclosed transport … raddr=('2001:67c:4e8:…', 443)` при прогоне `tests/acceptance` — сокет к серверу Telegram.
- Причина: `test_gf5_dry_run…` запускает `main.main()`, а `DRY_RUN` ещё не реализован → бот с токеном `1:x` звал `set_my_commands`/polling. Xfail не мешает тесту выполняться.
- Исправление: autouse-фикстура `_no_internet` в `tests/conftest.py` (getaddrinfo только для localhost) — любой случайный сетевой вызов теперь падает; в тесте Telegram и сборка диспетчера подменены на `AssertionError`.
- Регрессия: весь набор проходит с запретом сети (676 passed).

### B02 — токен API в тексте ошибки конфигурации
- Симптом: при неверном `SECOND_BRAIN_API_URL` ошибка старта содержала `input_value={…}` с хвостом `SECOND_BRAIN_API_TOKEN` и `BOT_TOKEN` → `docker logs`.
- Причина: pydantic добавляет входные данные в ошибку `model_validator(mode="after")`.
- Исправление: `hide_input_in_errors=True` в `model_config`; токен ещё и обрезается от пробелов.
- Регрессия: `tests/test_config_stage5.py::test_validation_error_never_shows_tokens`.

### B03 — `409 conflict` на POST становился «непредвиденной ошибкой» outbox
- Симптом: (найдено при написании стресс-теста) повтор с тем же ключом, пока первый запрос ещё выполняется, давал `_Conflict`, не `VaultError` → лог `exception` вместо обычного повтора.
- Исправление: `_Conflict` — подкласс `VaultUnavailable`; в контракте отдельный код `in_progress`.
- Регрессия: `tests/test_outbox_scenarios.py::test_stress_random_faults` (сбой `409` в наборе), `test_timeout_late_write_no_duplicate`.
