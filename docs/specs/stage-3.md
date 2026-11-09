# Этап 3 — удобство: кнопки, воскресный дайджест, запасной путь через Whisper

Статус: **план, ждёт «ок»** (09.11.2026).

## Задача
Под готовым конспектом в Telegram появляются кнопки 🔁 Подробнее / ✅ Прочитано / 🗑 Удалить, которые меняют заметку прямо в хранилище (commit + push). По воскресеньям в 18:00 бот присылает список непрочитанных конспектов. Если Gemini не может открыть видео (unlisted, слишком длинное, FAILED в Files API), бот сам расшифровывает звук через faster-whisper и делает конспект по тексту.

## Решения владельца (09.11.2026)
- **Подробнее** — заменяет ту же заметку (тот же файл, ссылки в Obsidian не ломаются). Один раз на заметку.
- **Whisper** — автоматически, только когда Gemini *не может открыть видео*. При 429/503 и дневной квоте — как сейчас, откладывание (конспект по тексту тоже идёт через Gemini, Whisper там не поможет).
- **Голосовое-идея → заметка** — не в этом этапе, в бэклог.
- **Удаление** — с подтверждением «Точно удалить?».

## Лимиты Gemini (gemini-3.5-flash-lite, 500/сутки)
| Действие | Запросы Gemini |
|---|---|
| ✅ Прочитано, 🗑 Удалить, дайджест, `/unread` | 0 |
| 🔁 Подробнее | +1 (кнопка исчезает после нажатия) |
| Whisper-путь | +1, только после того как видео не открылось |
| Подробнее/Whisper при перегрузке | откладывание, как у обычной задачи |

## Исследование
**Код.**
- Итоговое сообщение — правка статус-сообщения в `worker._edit` (без клавиатуры). Таблица `digests` хранит ключ → имя заметки, но не путь и не id, по которому можно сослаться из кнопки.
- `Vault.save` всегда создаёт новый файл; изменить или удалить существующий нечем. Блок commit+push живёт внутри `save`.
- `ReminderScheduler` уже владеет `AsyncIOScheduler(timezone=tz)` — туда же cron-задача дайджеста.
- `Transcriber.transcribe` жёстко `language="ru"` и возвращает сплошной текст без времени — для видео нужен автоопределение языка и сегменты с таймкодами.
- `DigestLLM._generate`: не-«перегружен» ошибка → `DigestError("Gemini не смог открыть …")`; `_wait_active` → `DigestError` при FAILED/таймауте. Отличить «видео не открылось» от прочих ошибок сейчас нельзя.
- У не-YouTube видео файл уже скачан — его можно сразу отдать Whisper (PyAV читает mp4). Для YouTube нужен yt-dlp `bestaudio` (m4a/webm, без ffmpeg).

**Библиотеки** (aiogram 3.31, APScheduler 3.11.3, faster-whisper 1.2.1, yt-dlp 2026.10.26; Context7).
- `CallbackData.pack()` бросает `ValueError`, если > 64 байт → в кнопке только `action` + числовой `id`.
- `bot.edit_message_text(..., reply_markup=...)` / `message.edit_reply_markup()` — смена клавиатуры.
- APScheduler: `add_job(fn, "cron", day_of_week="sun", hour=18, minute=0)`, таймзона из планировщика; `misfire_grace_time` + `coalesce=True`, чтобы бот, поднятый в 18:20, всё же прислал дайджест один раз.
- faster-whisper: `model.transcribe(path, language=None)` — автоопределение, сегменты с `start`.

**Интернет.**
- [faster-whisper benchmark](https://awesome.ecosyste.ms/projects/github.com%2FSYSTRAN%2Ffaster-whisper): `small` int8, 8 потоков — 13 мин аудио за ~1 м 42 с (~7× быстрее реального). На домашнем сервере медленнее → ограничиваем Whisper-путь 30 минутами видео.
- [Gemini forum: YouTube URL — intermittent 400 INVALID_ARGUMENT](https://discuss.ai.google.dev/t/youtube-url-video-input-returns-intermittent-400-invalid-argument-should-be-503-or-retryable-error-code/125883): 400 иногда случаен — Whisper-путь как раз страхует.
- [Gemini — Video understanding](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding): по ссылке — только публичные видео → unlisted уходит в Whisper.
- [Seal #2543](https://github.com/JunkFood02/Seal/issues/2543), [Render: yt-dlp sign-in problem](https://community.render.com/t/yt-dlp-sign-in-problem/26692): YouTube с серверных IP требует «Sign in to confirm you're not a bot» → понятная ошибка, cookies YouTube — не в этом этапе.

## Чего не хватало в плане (включено)
1. **Таблица `notes`** — id для кнопок, имя файла, url, chat. Кнопки переживают перезапуск.
2. **Источник правды о «прочитано» — файл**, а не БД: если отмечу `status: read` в Obsidian, дайджест это учтёт. Дайджест делает `git pull` и читает frontmatter.
3. **Заметку переименовали/переместили в Obsidian** → ищем в папке по `url:` во frontmatter; не нашли — «не нашёл заметку, переименована или перемещена?».
4. **Удалённая заметка** освобождает ссылку: ключи из `digests` удаляются, ссылку можно прислать снова.
5. **Двойное нажатие** «Подробнее» → «уже делаю»; «Удалить» без подтверждения ничего не делает.
6. **`/unread`** — тот же дайджест по запросу (0 запросов Gemini, удобно проверять руками).
7. **Whisper-конспект помечается**: `via: whisper` во frontmatter, `confidence` не выше `medium`, в промпте «экрана не видно». Сегменты передаются с метками `[MM:SS]` → таймкоды в заметке остаются.
8. **Кнопки и у «♻️ Уже есть»** — можно отметить прочитанным или удалить старую заметку.

Не делаю: голосовое-идея, Anki, RAG, кнопка «↩️ не прочитано», cookies для YouTube.

## План (файлы и порядок)
1. `database.py`: таблица `notes(id, note_name, url, source, chat_id, detailed, created_at)`; `digests.note_id`, `link_jobs.note_id` (миграция); `add_note`, `get_note`, `find_note(keys)`, `mark_detailed`, `rename_note`, `delete_note` (+ его ключи из `digests`).
2. `digest/vault.py`: общий `_commit_push`; `find(name, url)`, `set_status(name, url, status)`, `replace(name, url, markdown)`, `delete(name, url)`, `unread() -> list[UnreadNote]`.
3. `digest/errors.py`: `VideoUnreadable(DigestError)`; `digest/llm.py` бросает его из `digest_youtube`/`digest_video_file` (400/403/404 и FAILED/таймаут Files API); `digest_text(..., transcript=True)` — другой промпт.
4. `services/transcribe.py`: `transcribe_timed(path, language=None) -> str` со строками `[MM:SS] текст`.
5. `digest/fetchers.py`: `download_audio(url, out_dir, opts, max_seconds)` (bestaudio, проверка длительности до скачивания).
6. `digest/pipeline.py`: `make_note(..., transcriber=None)` — при `VideoUnreadable` и наличии transcriber → Whisper-путь; `render` получает `via`.
7. `digest/render.py` + шаблон: `via: whisper`.
8. `handlers/keyboards.py`: `NoteCb(action, id)`, `note_actions(id, detailed)`, `note_delete_confirm(id)`.
9. `digest/worker.py`: `Job.note_id` (Подробнее → `vault.replace`), `add_note` после save, клавиатура в итоговом и «Уже есть» сообщении, transcriber в deps.
10. `handlers/notes.py` (новый роутер, перед `context`): кнопки + `/unread`.
11. `digest/weekly.py`: `unread_text(notes)`; `scheduler.py`/`main.py`: cron вс 18:00 → `OWNER_ID`.
12. `config.py`: `whisper_fallback_max_seconds=1800`, `weekly_digest=True`. `digest/cli.py` — передать Transcriber. HELP, `CLAUDE.md`, `PLAN.md`.

## Критерии приёмки
**Кнопки**
- B1. Успешный конспект → итоговое сообщение с кнопками 🔁 Подробнее / ✅ Прочитано / 🗑 Удалить, callback ≤ 64 байт, в БД есть строка `notes`.
- B2. «✅ Прочитано» → в файле во frontmatter `status: unread` → `status: read` (больше ничего не меняется), commit + push; кнопка «Прочитано» исчезает. Строка `status: unread` в теле заметки не трогается.
- B3. «🗑 Удалить» → «Точно удалить?» [Да / Нет]; «Нет» возвращает прежние кнопки и ничего не удаляет; «Да» → файл удалён (commit + push), строка `notes` и её ключи `digests` удалены, сообщение «🗑 Удалено»; та же ссылка снова → новый конспект, а не «Уже есть».
- B4. «🔁 Подробнее» → задача в `link_jobs` с `note_id`, force, пожеланием «максимально подробно»; воркер перезаписывает **тот же** файл (имя не меняется, `status: unread`), `detailed=1`; повторное нажатие → «уже сделано / уже делаю», Gemini не вызывается.
- B5. Подробнее при перегрузке Gemini → откладывается как обычная задача; после перезапуска `note_id` восстанавливается.
- B6. Заметку переименовали в Obsidian → найдена по `url:` (имя в БД обновляется); не найдена → понятное сообщение, для «Прочитано»/«Подробнее» БД не меняется; для «Удалить» — «файла уже нет», строка из БД убирается.
- B7. «♻️ Уже есть» тоже с кнопками; несуществующий id → «Заметка не найдена».

**Дайджест**
- D1. Cron-задача вс 18:00 Europe/Bucharest, `coalesce`, `misfire_grace_time` 1 ч (спасает только от задержки event loop: задачи APScheduler в памяти, бот, перезапущенный после 18:00, пропустит неделю — есть `/unread`).
- D2. Текст: «📚 Непрочитанных конспектов: N» + 5 самых старых (эмодзи + заголовок, дата, мин чтения); N = 0 → по расписанию не отправляется, `/unread` отвечает «Всё прочитано 🎉».
- D3. «Прочитано» — по frontmatter файлов после `git pull` (файлы без `type: digest` и с `status: read` не считаются).
- D4. Дайджест не вызывает Gemini.

**Whisper**
- W1. YouTube: Gemini → 400 (не перегрузка) → yt-dlp bestaudio → Whisper (язык авто) → `digest_text(transcript=True)` → заметка с `via: whisper`, `confidence` ≤ medium, ссылки-таймкоды YouTube.
- W2. Instagram/TikTok: FAILED/таймаут Files API → Whisper по уже скачанному файлу, без второго скачивания.
- W3. 429/503/дневная квота → Whisper **не** запускается, задача откладывается (как сейчас).
- W4. Видео длиннее `WHISPER_FALLBACK_MAX_SECONDS` (1800) → понятная ошибка, аудио не скачивается.
- W5. Пустая расшифровка → «в видео нет речи — конспект не сделать»; ошибка yt-dlp → «Gemini не открыл видео, и звук скачать не вышло: …».
- W6. Статьи Whisper-путь не затрагивает; CLI без transcriber не падает.

**Общее**
- G1. `uv run pytest -q` и `uv run ruff check .` зелёные; сеть, Gemini, git-remote, yt-dlp и Whisper в тестах мокаются (git — локальный bare-репозиторий, как в `test_vault`).
