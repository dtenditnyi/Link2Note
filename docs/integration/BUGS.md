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

### B04 — `docker stop` убивал бота SIGKILL без штатной остановки
- Симптом (проверка F5 в Docker): SIGTERM от `docker stop` игнорировался, через 10 с — SIGKILL; `finally` в `main()` не выполнялся (воркер не отпускал задачу, outbox не останавливался).
- Причина: в контейнере бот — PID 1, а сигнал без обработчика PID 1 игнорирует.
- Исправление: `main.run()` ставит на SIGTERM `signal.default_int_handler` — как Ctrl+C. Проверено: `docker stop` — 1 с, в логе «Бот остановлен», exit 0.
- Регрессия: `tests/test_main_signals.py`.

### B05 — «Удалить» могло удалить чужую заметку (ревью C/D/E #1)
- Симптом: заметка ждёт в очереди под желаемым именем, на сайте такое имя уже занято другой заметкой; после восстановления сайт дал « (2)», а следующая операция группы ушла со старым именем и удалила/изменила чужую заметку.
- Причина: `Outbox.attempt` выполнял объекты, прочитанные до цикла (переименование в payload было только в БД); `ApiVault._locate` по имени не сверял ссылку.
- Исправление: перечитывание строки перед каждой попыткой; заметка, найденная по имени с другим `url:`, — не наша (ищем по url).
- Регрессия: `tests/test_outbox.py::test_attempt_uses_site_name_for_later_ops_no_foreign_delete`, `::test_locate_by_name_checks_url`.

### B06 — `/ask` мог не ответить ничем (ревью C/D/E #3)
- Симптом: длинный блок кода из `<>&`, кавычка в ссылке или разметка вперехлёст → невалидный HTML → `TelegramBadRequest`, пользователь без ответа.
- Исправление: резка по длине после экранирования, `&quot;` в href, URL не форматируются, проверка вложенности тегов, при ошибке Telegram — тот же текст без разметки.
- Регрессия: `tests/test_brain_commands.py` (5 тестов «ревью C/D/E»).

### B07 — офлайн-очередь сдавалась через сутки вместо недели (финальная проверка)
- Симптом: при умолчаниях операция уходила в `dead` через ~25 ч (30 попыток), хотя DEPLOY.md обещал ожидание `OUTBOX_MAX_AGE=7d`.
- Исправление: `OUTBOX_MAX_ATTEMPTS=200` по умолчанию.
- Регрессия: `tests/test_outbox.py::test_default_attempts_cover_max_age`.
