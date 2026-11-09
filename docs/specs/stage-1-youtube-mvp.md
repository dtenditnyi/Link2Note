# Этап 1 — MVP: YouTube → конспект → заметка в хранилище

Статус: **реализовано, ждёт ручной проверки** (план принят 09.11.2026). Ветка `feature/stage-1`.

## Задача
Кидаю боту ссылку на YouTube-видео или Shorts, можно с комментарием. Gemini смотрит видео по URL, без скачивания, и возвращает структурированный `Digest`. Из него получается Markdown-заметка по шаблону из PLAN §5, которая коммитится и пушится в клон хранилища. В Telegram приходит ответ: эмодзи, название, TL;DR и имя заметки. То же самое без Telegram: `python -m digest.cli <url>`.

## Решения владельца (09.11.2026)
- Модель — как в плане: `GEMINI_MODEL=gemini-3.8-flash` (общая настройка с напоминаниями).
- Очередь — `asyncio.Queue` в памяти, без БД. После перезапуска очередь теряется, это допустимо.
- Ручная проверка vault — `../vault-test`: bare-репозиторий и его клон. **Настоящий myObsidian не трогаем.**
- Ссылки не с YouTube (статьи, Reels, TikTok) → ответ «пока умею только YouTube — остальное на этапе 2».
- Комментарий рядом со ссылкой уходит в Gemini как подсказка уже сейчас, хотя по плану это этап 2 (решение владельца).
- Автор и название берутся из **YouTube oEmbed**; это же заодно проверяет, что видео доступно. Длительности нет — `duration` не выводится.
- Футер заметки без ссылки на MOC: `🔗 Источник: [YouTube](url)`.

## Исследование
**Код.**
- `services/intent.py` — готовый образец работы с Gemini: клиент передаётся в конструктор (тестируемо, без ключа при импорте), structured output и разбор `resp.text` через `model_validate_json` с понятной ошибкой.
- Порядок роутеров `basic → reminders → voice → context` задан в `main.build_dispatcher`. `handlers/context.py` уже пропускает текст со ссылками (и `url`/`text_link`-entities) «для Link2Note».
- `on_free_text` в `reminders` перехватывает текст со словом «напомни» раньше links. Так и оставляем: «напомни посмотреть https://…» — это напоминание.
- В Dockerfile уже есть `git`. В compose нет томов `vault`/`secrets`.

**Библиотеки** (google-genai 3.0.0, aiogram 3.31; проверено по исходникам в `.venv` и через Context7 HTTP API).
- YouTube передаётся как `types.Part(file_data=types.FileData(file_uri=url))`. `Part.from_uri` для YouTube **не годится**: без `mime_type` он угадывает тип по расширению и падает с `ValueError`.
- `GenerateContentConfig.media_resolution=MediaResolution.MEDIA_RESOLUTION_LOW` существует. `http_options=HttpOptions(timeout=…, retry_options=HttpRetryOptions(attempts=…, http_status_codes=[429, 503]))` даёт встроенные ретраи.
- ~~`t_schema` в SDK 3.0 сам убирает `default`~~ — **неверно, исправлено на этапе кода** (тест L4 поймал): SDK пропускает `default` у полей вложенных моделей (`windows: str = ""`) в запрос к Gemini. Работает так: pydantic не пишет `default` в JSON-схему для `Field(default_factory=…)`, а поле при этом становится необязательным. Поэтому все необязательные поля `Digest` и вложенных моделей заданы через `default_factory`.
- `resp.parsed` **молча** глотает `ValidationError` и превращается в `None`. Поэтому разбираем `resp.text` сами, как в `intent.py`.
- aiogram: `MessageEntity.extract_from(text)` корректно режет UTF-16 (эмодзи). У `text_link` URL лежит в `entity.url`, у подписей — `caption_entities`.

**Интернет.**
- [Gemini — Video understanding](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding): только публичные видео (не unlisted и не private), до 8 ч YouTube в день, функция в preview.
- [Форум Gemini: pydantic `default` → «Unknown field for Schema: default»](https://discuss.ai.google.dev/t/response-schema-from-pydantic/50028): актуально и для SDK 3.0 у вложенных моделей — решено через `default_factory` (см. выше).
- [Gemini — structured output](https://ai.google.dev/gemini-api/docs/json-mode): модель может пропустить поле-список. Списки делаем необязательными, со значением по умолчанию `[]`.
- [pensar changelog](https://cran.nics.utk.edu/cran/web/packages/pensar/news/news.html) и [Mercurio git sync](https://community.obsidian.md/plugins/mercurio-git-sync): автопуш делает одну-две попытки `pull --rebase`, при настоящем конфликте выполняет `rebase --abort` и **никогда не оставляет репозиторий посреди rebase**; force-push не используется.

## Чего не хватало в плане (включено в этап)
1. **Ссылки в entities.** В пересланных постах и «ссылке под словом» URL лежит в `text_link`, а не в тексте. Берём URL из `entities`/`caption_entities` плюс regex по тексту.
2. **Хвостовая пунктуация.** В «смотри https://youtu.be/ID).» regex из плана захватит `).`. Обрезаем `.,;:!?)]}»"'`.
3. **Канонический URL.** Для Gemini, ссылки в заметке и таймкодов — `https://www.youtube.com/watch?v=ID` / `https://youtu.be/ID?t=N`. Мусор вроде `?si=`, `&list=`, `&t=` убираем. Поддерживаем `watch`, `youtu.be`, `shorts/`, `live/`, `embed/`, `m.`/`music.`/`youtube-nocookie.com`. Ссылка на YouTube без ID видео (канал, плейлист) → понятный отказ.
4. **HTML-экранирование.** Ответ бота идёт с `parse_mode=HTML`, а заголовок от ИИ может содержать `<`/`&`. Иначе Telegram отклонит сообщение.
5. **YAML и Markdown в заметке.** Строки во frontmatter экранируем (title/author с `:` и `"`); `|` в ячейках таблицы → `\|`; если код содержит ```` ``` ````, забор делаем длиннее.
6. **Воркер не должен умирать.** Ошибка внутри обработки ошибки (например, сообщение-статус удалено) логируется, следующая задача продолжает выполняться. Пользователь видит понятную причину, а не трассировку.
7. **Git без интерактива.** Автор коммитов передаётся через `-c user.name/user.email` (в контейнере нет глобального конфига). Конфликт rebase → `rebase --abort`, заметка остаётся закоммиченной локально, в ответе об этом сказано честно. Пуш до 3 попыток.
8. **Хранилище не настроено** (`VAULT_REPO` пуст или не git-репозиторий): напоминания работают как раньше, а на ссылку бот отвечает «хранилище не настроено».
9. **Лимит ссылок:** не больше 5 из одного сообщения, остальные с предупреждением.
10. **Таймаут Gemini** — 300 с; ретрай при 429/503 — 3 попытки (встроенный в SDK).

Не делаю (этапы 2+): дубликаты и таблица `digests`, yt-dlp, статьи, кнопки, дайджест, запасной путь через Whisper.

## План (файлы и порядок)
1. `config.py`: `vault_repo: Path | None = None`, `vault_folder = "03 - Resources/📥 Из ссылок"`, `git_author_name = "Link2Note"`, `git_author_email = "link2note@localhost"`, `gemini_timeout_s = 300`.
2. `digest/classifier.py`: `Source`, `classify`, `normalize`, `youtube_id`, `canonical_youtube`, `extract_urls(text, entities)`.
3. `digest/schema.py`: `Digest` и вложенные модели; `confidence: Literal["high","medium","low"]`; списки — `Field(default_factory=list)`.
4. `digest/fetchers.py`: `youtube_meta(url, client) -> VideoMeta(title, author)` через `https://www.youtube.com/oembed?url=…&format=json`; 401/403 → «видео приватное или с ограничениями», 404 → «видео не найдено».
5. `digest/llm.py`: `ALLOWED_TAGS`, `SYSTEM`, `DigestLLM(api_key, model, timeout, client=None).digest_youtube(url, hint)`; разбор через `model_validate_json`; `_clean` (теги из списка, не больше 3, иначе `personal`; `read_minutes ≥ 1`); `DigestError` с понятным текстом.
6. `digest/render.py` + `digest/templates/note.md.j2`: `to_markdown(d, *, url, source, author, date_added) -> str`.
7. `digest/vault.py`: `Vault(repo, folder, author_name, author_email)`, `safe_filename`, `save(title, md) -> имя заметки`; `VaultError`.
8. `digest/pipeline.py`: `make_note(url, hint, llm, http, today) -> (Digest, markdown)` — общий путь для CLI и воркера.
9. `digest/cli.py`: `python -m digest.cli <url> [--hint …] [--save]` печатает Markdown; с `--save` дополнительно сохраняет в `VAULT_REPO`.
10. `digest/worker.py`: `Job`, `run_worker(queue, bot, …)`; статусы «⏳ В очереди…» → «🧠 Смотрю видео…» → итог.
11. `handlers/links.py`: роутер `links`, фильтр «есть URL» (текст или подпись), разбор подсказки, постановка в очередь, ответы для не-YouTube и для ненастроенного хранилища.
12. `main.py`: роутеры `basic → reminders → voice → links → context`; в `workflow_data` — `link_queue` и `vault_ready`; воркер запускается `asyncio.create_task` и отменяется при остановке.
13. `handlers/basic.py` (HELP), `handlers/context.py` (docstring), `docker-compose.yml` (`./vault:/vault`, `./secrets:/secrets:ro`, `GIT_SSH_COMMAND` в `.env`), `CLAUDE.md`, `PLAN.md`.

Тесты (сначала, красные): `test_classifier.py`, `test_schema_llm.py`, `test_fetchers.py` (`httpx.MockTransport`), `test_render.py` + `tests/golden/*.md`, `test_vault.py` (локальный `git init --bare` во `tmp_path`, без сети), `test_worker.py`, `test_links_handlers.py`, правки `test_main.py` и `test_config.py`.

## Что нужно от владельца
- Сейчас ничего: Gemini-ключ уже есть, ручная проверка — на `../vault-test`.
- Для деплоя (вне этапа, когда решишь): клон myObsidian на сервере, deploy key, `.env`: `VAULT_REPO=/vault`, `GIT_SSH_COMMAND=ssh -i /secrets/deploy_key -o IdentitiesOnly=yes`.

## Критерии приёмки
**Классификатор (без сети)**
- C1. `youtu.be/ID`, `youtube.com/watch?v=ID&si=x&t=5`, `m.youtube.com/watch?v=ID`, `youtube.com/shorts/ID`, `youtube.com/live/ID`, `youtube.com/embed/ID`, `music.youtube.com/watch?v=ID` → `Source.YOUTUBE`, одинаковый `normalize` = `youtube:ID` и канонический URL `https://www.youtube.com/watch?v=ID`.
- C2. instagram.com/reel/…, tiktok.com/@u/video/…, x.com/…, статья → `INSTAGRAM`/`TIKTOK`/`VIDEO`/`ARTICLE`. Не YouTube, но похоже: `notyoutube.com`, `youtube.com.evil.io` → `ARTICLE`.
- C3. `youtube.com/@channel` и `youtube.com/playlist?list=…` → `youtube_id is None`.
- C4. `extract_urls`: «смотри https://youtu.be/ID).» → `https://youtu.be/ID`; `text_link` с `url` → найден; дубликаты в одном сообщении не повторяются; порядок сохраняется.

**LLM (Gemini замокан)**
- L1. `digest_youtube` вызывает `generate_content` с моделью из настроек, частью `file_data.file_uri` = канонический URL, `response_mime_type=application/json`, `response_schema=Digest`, `media_resolution=LOW`, `temperature=0.2`; подсказка попадает в текст запроса.
- L2. Теги вне `ALLOWED_TAGS` выкидываются, остаётся не больше 3; если пусто — `["personal"]`.
- L3. Пустой ответ или невалидный JSON → `DigestError` с текстом на русском; ответ без необязательных списков → валидный `Digest` с `[]`.
- L4. В JSON-схеме, отправленной в Gemini, нет ключей `default`, а `required` = `title, emoji, tldr, key_points, tags, read_minutes, confidence`.

**oEmbed (httpx замокан)**
- F1. 200 → `VideoMeta(title, author)`; 401/403 → `DigestError` «приватное/ограниченное»; 404 → «не найдено»; сетевая ошибка → метаданные пустые, обработка продолжается.

**Рендер**
- R1. Полный `Digest` → совпадает с golden-файлом `tests/golden/full.md` (структура PLAN §5, футер без MOC).
- R2. Минимальный `Digest` (только обязательные поля) → нет разделов «Горячие клавиши», «Пошагово», «Команды и код», «Ресурсы», «Что сделать», «Таймкоды»; нет пустых строк подряд больше одной.
- R3. Заголовок `Fix: "quotes" & #tags` и автор `A: B` → frontmatter валиден (`yaml.safe_load` или эквивалентная проверка); `|` в горячей клавише экранирован; код с ```` ``` ```` обёрнут более длинным забором.
- R4. Таймкод 135 с → `[02:15](https://youtu.be/ID?t=135)`, 3725 с → `[1:02:05](…)`.

**Vault (локальный bare-репозиторий)**
- V1. `save()` создаёт `<folder>/YYYY-MM-DD <title>.md`, коммит с автором Link2Note, пуш в bare — файл виден в `git log` bare-репозитория.
- V2. Имя файла: `/ \ : * ? " < > | # ^ [ ]` убраны, обрезка до 80 символов, без точки или пробела в конце; повтор того же заголовка в тот же день → `… (2).md`.
- V3. Кто-то запушил в bare другой файл → `save()` подтягивает его (rebase) и пушит успешно.
- V4. Конфликт или пуш невозможен → `VaultError`, репозиторий **не** в состоянии rebase (`.git/rebase-merge` отсутствует).
- V5. Два одновременных `save()` → оба файла в bare, без ошибок (lock).

**Telegram (aiogram, Telegram API замокан)**
- T1. Владелец: «https://youtu.be/ID сделай упор на хоткеи» → reply «⏳…», в очереди `Job(url, hint="сделай упор на хоткеи", chat_id, status_msg_id)`.
- T2. Ссылка в подписи к пересланному посту / в `text_link` → тоже в очередь.
- T3. Ссылка на статью или Reels → ответ «пока только YouTube», в очередь не попадает.
- T4. Текст без ссылки → по-прежнему тихо уходит в контекст (тест `test_plain_text_goes_to_context_silently` зелёный), links его не трогает.
- T5. Чужой user_id со ссылкой → ни одного вызова Telegram API, очередь пуста.
- T6. Порядок роутеров: `basic, reminders, voice, links, context`.
- T7. `VAULT_REPO` не настроен → ответ «хранилище не настроено», в очередь не ставится.
- T8. Больше 5 ссылок → в очередь первые 5 + предупреждение.

**Воркер**
- W1. Успех → статус-сообщение отредактировано: `{emoji} <b>{title}</b>\n\n{tldr}\n\n📝 Заметка: «{name}»`, где title/tldr HTML-экранированы.
- W2. `DigestError`/`VaultError` → «❌ Не получилось: <понятная причина>»; неожиданное исключение → «❌ Не получилось: внутренняя ошибка» + лог.
- W3. Падение `edit_message_text` внутри обработки ошибки не останавливает воркер: следующая задача обрабатывается.
- W4. Задачи выполняются по одной, по порядку.

**CLI и проверка**
- K1. `uv run python -m digest.cli <url>` печатает Markdown и ничего не пишет на диск; `--save` сохраняет в `VAULT_REPO`.
- K2. `uv run pytest -q` и `uv run ruff check .` — зелёные.
- K3. Ручная проверка: живой прогон CLI на коротком публичном видео; бот с `VAULT_REPO=../vault-test/clone` → заметка в bare-репозитории `../vault-test`.

## После ревью (09.11.2026)
- git: таймаут 120 с на каждую команду; при таймауте или отмене git убивается вместе с детьми (на Linux — вся группа процессов, включая ssh); ожидание после kill ограничено, pipe закрывается. Тест: хук «висит» 20 с, запись падает с VaultError за ~6 с.
- Конфликт rebase с неотправленным локальным коммитом блокирует push до ручного вмешательства — пишем `log.error` с командой для восстановления.
- Ошибки Gemini: 429 → «лимит исчерпан», прочие 4xx → «не смог открыть видео», 5xx → «Gemini недоступен» (раньше — «внутренняя ошибка»).
- Подсказка: у пересланных постов не берётся (чужой текст), обрезается до 300 символов.
- Одно видео в разных формах ссылки в одном сообщении → одна задача.
- `docker-compose.yml`: `BatchMode=yes`, `ConnectTimeout=15` для ssh; `chmod 600` и `chown 1000` ключа на хосте.
