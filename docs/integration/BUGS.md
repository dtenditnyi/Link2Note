# Этап 5 — найденные баги

Формат: симптом → причина → исправление → регрессионный тест.

### B01 — xfail-тест приёмки ходил в настоящий Telegram
- Симптом: `ResourceWarning: unclosed transport … raddr=('2001:67c:4e8:…', 443)` при прогоне `tests/acceptance` — сокет к серверу Telegram.
- Причина: `test_gf5_dry_run…` запускает `main.main()`, а `DRY_RUN` ещё не реализован → бот с токеном `1:x` звал `set_my_commands`/polling. Xfail не мешает тесту выполняться.
- Исправление: autouse-фикстура `_no_internet` в `tests/conftest.py` (getaddrinfo только для localhost) — любой случайный сетевой вызов теперь падает; в тесте Telegram и сборка диспетчера подменены на `AssertionError`.
- Регрессия: весь набор проходит с запретом сети (676 passed).
