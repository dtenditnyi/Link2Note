# Этап 2 — все источники: Reels/TikTok/X, статьи, дубликаты, очередь в SQLite

Статус: **код готов, ждёт ручной проверки** (план принят 09.11.2026).

## Задача
Бот принимает не только YouTube. Видео из Instagram, TikTok, X, Vimeo, Reddit и Facebook скачивается через `yt-dlp` и уходит в Gemini Files API (картинка + звук). Обычная веб-страница превращается в текст через `trafilatura`, конспект делается по нему. Повторная ссылка не тратит квоту: бот отвечает «♻️ Уже есть: «заметка»», а слово «заново» в комментарии делает конспект ещё раз. Очередь ссылок теперь хранится в SQLite и переживает перезапуск. Плюс два пункта бэклога этапа 1: замеры времени по шагам и точные теги.

## Решения владельца (09.11.2026)
- **Дубликаты:** по умолчанию «♻️ Уже есть», но если в комментарии есть слово «заново» — проверка пропускается (само слово в подсказку для Gemini не идёт). Кнопка 🗑 — этап 3.
- **Теги:** новый тег `fun` (юмор, мультфильмы, развлечения). В промпте — описание смысла каждого тега; `books` только для материала про книги.
- **Instagram:** `COOKIES_FILE` необязателен. Без него бот пробует так, а на «login required» отвечает понятной ошибкой «нужны cookies». Cookies — со второго аккаунта (риск бана).
- **Очередь — в SQLite** (таблица `link_jobs`): после перезапуска недоделанные задачи снова встают в очередь.

## Исследование
**Код.**
- `digest/pipeline.make_note` сейчас обрабатывает только YouTube; `handlers/links.py` отсекает всё, кроме YouTube. `render` строит ссылки-таймкоды только для YouTube, `_SOURCE_LABELS` знает только youtube.
- `classifier.normalize` для не-YouTube берёт `host + path` без query: для `news.ycombinator.com/item?id=1` и `?id=2` ключ совпадёт → ложный дубликат. Instagram `/reel/ID`, `/reels/ID`, `/p/ID` и TikTok `vm.tiktok.com/xyz` (короткая ссылка) дают разные ключи для одного ролика.
- `Database` — синхронный sqlite3, вызывается из async-кода (так уже сделаны напоминания, запросы мгновенные). Таблицы `digests` пока нет.
- В Dockerfile есть `ffmpeg`, локально на Windows ffmpeg нет → формат выбираем «один файл со звуком», слияние дорожек — только запасной вариант.

**Библиотеки** (yt-dlp 2026.10.26, trafilatura 2.3.1, google-genai 3.0.0 — по исходникам в `.venv` и Context7).
- yt-dlp синхронный → `asyncio.to_thread`. `extract_info(download=False)` до скачивания — проверка длительности. Ошибки — `yt_dlp.utils.DownloadError`.
- **yt-dlp перезаписывает `cookiefile` при закрытии** (`YoutubeDL.close() → save_cookies() → cookiejar.save()`). На томе `/secrets:ro` это упадёт, а на обычном — тихо изменит мой файл. Поэтому копируем cookies во временную папку задачи.
- Instagram-экстрактор использует impersonation, если доступен `curl_cffi` (сейчас не установлен). Предлагаю `yt-dlp[curl-cffi]`.
- `client.aio.files.upload(file=path, config={"mime_type": …})` → `File` со `state` `PROCESSING | ACTIVE | FAILED`; ждём `files.get` до `ACTIVE`, затем `Part.from_uri(file_uri=f.uri, mime_type=f.mime_type)`, в конце `files.delete`.
- `trafilatura.bare_extraction(html_bytes, url=…, with_metadata=True)` → `Document` (`text`, `title`, `author`, `sitename`); принимает bytes и сам угадывает кодировку. CPU-работа → `to_thread`.

**Интернет.**
- [Gemini — Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding): после upload ждать `ACTIVE`, обрабатывать `FAILED`; в примерах нет таймаута — добавляем свой.
- [Форум Gemini: «file is in PROCESSING state»](https://discuss.ai.google.dev/t/file-api-upload-video-switch-from-activate-to-processing/109055): запрос к ещё не готовому файлу падает — без ожидания нельзя.
- [faster-whisper-webui: Instagram «login required or rate-limit»](https://huggingface.co/spaces/aadnk/faster-whisper-webui/discussions/4): без входа Reels почти не качаются → cookies.
- [Seal #2566](https://github.com/JunkFood02/Seal/issues/2566): Instagram в yt-dlp 2026 падает без impersonate-target → `curl_cffi`.
- [yt-dlp wiki: Extractors](https://lab.trianta.dev/mirrors/yt-dlp.yt-dlp/wiki/Extractors): с аккаунтом есть риск бана, лучше запасной аккаунт и редкие запросы.

## Чего не хватало в плане (включено в этап)
1. **Cookies копируются** во временную папку (см. выше), исходный файл не меняется.
2. **Понятные ошибки yt-dlp:** «login required / rate-limit» → «Instagram требует вход — добавь COOKIES_FILE» (или «cookies устарели», если файл задан); «нет видео в посте»; длиннее часа; «не смог скачать: <последняя строка>».
3. **Статьи:** лимит 5 МБ (читаем потоком), PDF/картинки/не-HTML → «пока умею только веб-страницы»; 401/402/403 → «закрыто (вход или paywall)»; 404 → «страница не найдена»; текста < 300 символов → «не смог вытащить текст (сайт на JavaScript или paywall)». Текст обрезается до 200 000 символов. User-Agent — обычный браузерный, без личных данных.
4. **Нормализация для дубликатов:** query сохраняется, но без мусора (`utm_*`, `fbclid`, `gclid`, `igsh`, `igshid`, `si`, `ref`, `share*`…), параметры сортируются, `#fragment` отбрасывается. Instagram `/reel|reels|p|tv/ID` → `instagram:ID`, TikTok `/video/ID` → `tiktok:ID`. После скачивания добавляется второй ключ `<extractor>:<id>` от yt-dlp — так `vm.tiktok.com/xyz` и полная ссылка тоже совпадут.
5. **Files API:** таймаут ожидания `ACTIVE` 180 с; файл удаляется в `finally` даже при ошибке (сбой удаления — только лог; Google сам удалит через 48 ч). MIME-тип передаём явно.
6. **Временные файлы** — `TemporaryDirectory` на задачу, удаляется всегда.
7. **Защита очереди от зацикливания:** задача, на которой бот падал 3 раза подряд (например, OOM), при старте помечается `failed` с сообщением, а не повторяется вечно.
8. **Ответы в Telegram по типу:** «⬇️ Скачиваю видео…» → «🧠 Смотрю видео…», «📖 Читаю статью…» → «🧠 Делаю конспект…».

Не делаю (этапы 3+): кнопки, дайджест, запасной путь через Whisper, видео-файл, присланный в бот, книга по названию, PDF.

## План (файлы и порядок)
1. `pyproject.toml`: `yt-dlp[curl-cffi]`; `uv lock`.
2. `config.py`: `cookies_file: Path | None`, `video_max_seconds = 3600`, `video_max_mb = 300`.
3. `digest/classifier.py`: новый `normalize` (п. 4), `is_force(hint) / strip_force(hint)` для «заново».
4. `database.py`: таблицы `digests(key PK, note_name, url, source, created_at)` и `link_jobs(id, url, hint, force, chat_id, status_msg_id, status pending|done|failed, attempts, created_at)`; методы `find_digest(keys)`, `save_digest(keys, note, url, source)`, `add_job`, `start_job` (attempts+1), `finish_job`, `pending_jobs`.
5. `digest/fetchers.py`: `download_video(url, out_dir, *, cookies_file, max_seconds, max_mb) -> (Path, VideoMeta)` (`VideoMeta` + `duration`, `key`); `fetch_article(url, http) -> Article(title, author, sitename, text)`.
6. `digest/llm.py`: `fun` в `ALLOWED_TAGS`, описания тегов в `SYSTEM`, промпт про «материал», а не только «видео»; `digest_video_file(path, mime, hint)`, `digest_text(text, title, hint)`; общий маппинг ошибок Gemini.
7. `digest/render.py` + шаблон: подписи источников (Instagram, TikTok, Видео, Статья), `duration: "MM:SS"` во frontmatter, если известна; у статьи таймкодов нет; у не-YouTube таймкоды без ссылок.
8. `digest/pipeline.py`: `make_note` выбирает путь по `Source`, возвращает `Note(digest, markdown, keys)`; замеры времени каждого шага в лог (`⏱ download 12.3 с`).
9. `digest/worker.py`: `Job` с `id` и `force`; дубликат → «♻️ Уже есть: «…»»; успех/ошибка → `finish_job`; `VaultError` с `note_name` тоже записывает дубликат; общий замер времени задачи; `restore_jobs(db, queue, bot)` при старте.
10. `handlers/links.py`: принимает любые ссылки (кроме YouTube-канала или плейлиста — для них отдельный ответ); один ключ `normalize` в сообщении → одна задача; задача сначала пишется в БД, потом в очередь.
11. `digest/cli.py`: все источники (без БД и без дубликатов), справка.
12. `main.py`: передать `db`/настройки в воркер, восстановить очередь при старте. `handlers/basic.py` HELP, `docker-compose.yml`/`.env`-комментарий про `COOKIES_FILE`, `CLAUDE.md`, `PLAN.md`.

Тесты (сначала, красные): `test_classifier.py`, `test_database.py`, `test_fetchers.py` (yt-dlp — фейковый `YoutubeDL`, httpx — `MockTransport`), `test_schema_llm.py`, `test_render.py` (+ golden для статьи), `test_worker.py`, `test_links_handlers.py`, `test_cli.py`, `test_main.py`.

## Что нужно от владельца
- Для ручной проверки Instagram (по желанию): `cookies.txt` со второго аккаунта (расширение «Get cookies.txt LOCALLY»), путь — в `COOKIES_FILE` в `.env`. Не коммитить.
- Для локальной проверки слияния дорожек — ffmpeg (не обязательно: обычно у Reels/TikTok есть готовый mp4 со звуком).

## Критерии приёмки
**Классификатор / нормализация**
- N1. `instagram.com/reel/ID/?igsh=x`, `/reels/ID`, `/p/ID`, `/user/reel/ID` → `instagram:ID`; `tiktok.com/@u/video/123?is_from_webapp=1` → `tiktok:123`.
- N2. `example.com/a?utm_source=x&b=2&a=1#top` и `example.com/a/?a=1&b=2` → один ключ; `news.ycombinator.com/item?id=1` ≠ `…?id=2`.
- N3. YouTube-ключи не изменились (тесты C1 зелёные).
- N4. «заново» (любой регистр, отдельным словом) → `force=True`, слово убрано из подсказки; «зановой»/«позаново» не срабатывает.

**База**
- D1. `save_digest(["tiktok:1", "tiktok:abc"], note)` → `find_digest(["tiktok:abc"])` возвращает note; неизвестный ключ → `None`; повторное сохранение того же ключа — новая заметка (последняя).
- D2. `add_job` → `pending_jobs()` содержит её; `start_job` увеличивает `attempts`; `finish_job(id, "done")` → больше нет в `pending_jobs`. `init()` на старой базе (только reminders) добавляет таблицы, напоминания целы.

**Загрузка видео (yt-dlp замокан)**
- Y1. Длительность > `video_max_seconds` → `DigestError` до скачивания (download не вызывался).
- Y2. Cookies: в `YoutubeDL` передаётся **копия** во временной папке; исходный файл не изменён, даже если yt-dlp его перезаписал.
- Y3. `DownloadError("… login required …")` → `DigestError` про cookies (текст различается: файла нет / файл задан); «No video formats found» → «в посте нет видео»; прочее → «не смог скачать видео: <последняя строка>».
- Y4. Успех → путь к файлу, `VideoMeta(title, author=uploader, duration, key="<extractor>:<id>")`; формат — `b[height<=720]` первым.

**Статьи (httpx замокан)**
- A1. HTML-страница → `Article` с текстом и заголовком/автором из trafilatura.
- A2. 403 → «закрыто», 404 → «не найдена», `application/pdf` → «только веб-страницы», ответ > 5 МБ → ошибка без чтения целиком, текст < 300 символов → «не смог вытащить текст».

**LLM (Gemini замокан)**
- L5. `digest_video_file`: upload с явным `mime_type` → опрос до `ACTIVE` → `generate_content` с `file_uri` → `files.delete` вызван. `FAILED` или таймаут ожидания → `DigestError`, и `delete` всё равно вызван.
- L6. `digest_text` отправляет заголовок и текст, обрезанный до 200 000 символов; подсказка в запросе.
- L7. 429 → «лимит», 5xx → «недоступен» — для всех трёх методов.
- L8. `fun` в `ALLOWED_TAGS`; в `SYSTEM` у каждого тега есть описание, у `books` — «только если материал про книгу»; `clean(["fun"])` → `["fun"]`, `clean(["dev","ai"])` не меняется.
- L4 (этапа 1) по-прежнему зелёный.

**Рендер**
- R5. Статья → `source: article`, автор (или сайт, если автора нет), нет `duration`, нет раздела «Таймкоды», футер `🔗 Источник: [Статья](url)`; совпадает с `tests/golden/article.md`.
- R6. TikTok с `duration=522` → `duration: "08:42"`, таймкоды без ссылок, футер `[TikTok](url)`.
- R1–R4 этапа 1 зелёные (golden YouTube не меняется).

**Конвейер и воркер**
- P1. `make_note` выбирает путь: YouTube → `digest_youtube`; Instagram/TikTok/VIDEO → `download_video` + `digest_video_file`; ARTICLE → `fetch_article` + `digest_text`. Временная папка удалена и при успехе, и при ошибке.
- P2. В лог пишется время каждого шага и общее время задачи.
- W5. Ссылка уже в `digests` → «♻️ Уже есть: «note»», LLM не вызывался, `vault.save` не вызывался. С `force=True` — конспект делается заново.
- W6. После успеха в `digests` записаны все ключи (`normalize` + ключ yt-dlp). `VaultError` с `note_name` → тоже записан.
- W7. Каждая задача после обработки (успех, `DigestError`, неожиданная ошибка) → `finish_job`; статусы по типу источника (п. 8).
- W8. `restore_jobs`: pending с `attempts < 3` → снова в очереди + статус «⏳ После перезапуска снова в очереди…»; с `attempts ≥ 3` → `failed` + «❌ Не получилось: бот падал на этой ссылке 3 раза».
- W1–W4 этапа 1 зелёные.

**Telegram**
- T3'. Статья, Reels, TikTok → в очереди (и в `link_jobs`); YouTube-канал/плейлист → отказ «пришли ссылку на ролик», в очередь не идёт.
- T9. `instagram.com/reel/ID` и `instagram.com/reel/ID/?igsh=x` в одном сообщении → одна задача.
- T10. «https://… заново» → `Job.force=True`, `hint=""`.
- T1, T2, T4–T8 этапа 1 зелёные (тексты T3/T8 обновлены).

**CLI и проверка**
- K4. `python -m digest.cli <статья>` и `<tiktok>` печатают Markdown (через тот же `make_note`).
- K2. `uv run pytest -q` и `uv run ruff check .` — зелёные.
- K5. Ручная проверка на `../vault-test`: статья (habr/блог), TikTok, Reels (с cookies или проверка понятной ошибки без них), X-видео; повтор ссылки → «уже есть»; «заново» → новая заметка; перезапуск бота с задачей в очереди → задача доделана; тег мультфильма — `fun`.

## После ревью (09.11.2026)
- Штатная остановка бота посреди задачи (`CancelledError`) больше не помечает её `failed`: задача остаётся `pending`, попытка не засчитывается (`release_job`), после старта `restore_jobs` её вернёт.
- yt-dlp: одно извлечение (`extract_info(download=False)` → `process_ie_result(download=True)`), а не два — меньше запросов к Instagram/TikTok. Путь к файлу — из `requested_downloads`, а не поиском по папке (мог взять `.part`). Карусель/плейлист → первое видео.
- Пересланный пост: только ссылки на видео, а если их нет — первая статья (без `t.me`); только `t.me` → «нет ссылок, которые я умею разбирать». Свои сообщения — все ссылки, как раньше.
- Сообщение про cookies без упоминания Instagram (подходит и для X/Facebook). Общий таймаут загрузки статьи 30 с.
- Не делал: второй ключ дубликата для статьи по адресу после редиректа — мелочь, можно позже.
- Тестовые данные: абзацы в тестовом HTML сделаны разными (trafilatura схлопывает одинаковые); регулярки сообщений — без учёта регистра.
