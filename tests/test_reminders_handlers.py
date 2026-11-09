from datetime import datetime, timedelta

import pytest

from handlers import basic
from handlers import reminders as h
from scheduler import ReminderScheduler
from services.timeparse import Repeat
from tests.conftest import OWNER, STRANGER, TZ, make_callback, make_message, sent_texts


@pytest.fixture
def sched(bot, db, aps, now):
    return ReminderScheduler(bot, db, TZ, scheduler=aps, clock=lambda: now)


@pytest.fixture
def ctx(db, sched, now):
    return {"db": db, "reminders": sched, "tz": TZ, "clock": lambda: now}


# --- Создание (FSM): текст → время → повтор ----------------------------------------


async def test_full_create_flow(state, ctx, db, aps, now):
    await h.start_create(make_message(h.CREATE_BUTTON), state)
    assert await state.get_state() == h.CreateReminder.task.state

    await h.on_task(make_message("Выпить <воды>"), state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.time.state

    msg = make_message("через 20 мин")
    await h.on_time(msg, state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.repeat.state
    assert msg.answer.call_args.kwargs["reply_markup"] is not None  # кнопки повтора (R1)

    cb = make_callback()
    await h.on_repeat(cb, h.RepeatCb(value="daily"), state, **_only(ctx, "db", "reminders", "tz"))
    [r] = db.list_pending(OWNER)
    assert (r.task, r.repeat, r.remind_at) == (
        "Выпить <воды>",
        Repeat.DAILY,
        now + timedelta(minutes=20),
    )
    assert f"reminder-{r.id}" in aps.jobs
    assert await state.get_state() is None
    text = sent_texts(cb.message.edit_text)[0]
    assert "Выпить &lt;воды&gt;" in text  # A5
    assert "ежедневно" in text


async def test_empty_task_asks_again(state, now):
    await state.set_state(h.CreateReminder.task)
    msg = make_message("   ")
    await h.on_task(msg, state, tz=TZ, clock=lambda: now)
    assert await state.get_state() == h.CreateReminder.task.state
    msg.answer.assert_called_once()


async def test_task_from_non_text_message_asks_again(state, now):
    await state.set_state(h.CreateReminder.task)
    await h.on_task(make_message(None), state, tz=TZ, clock=lambda: now)
    assert await state.get_state() == h.CreateReminder.task.state


@pytest.mark.parametrize("bad", ["когда-нибудь", "2020-01-01 10:00"])
async def test_bad_time_keeps_state_and_explains(state, ctx, bad):
    await state.set_state(h.CreateReminder.time)
    await state.update_data(task="x")
    msg = make_message(bad)
    await h.on_time(msg, state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.time.state
    assert "HH:MM" in sent_texts(msg.answer)[0] or "прошл" in sent_texts(msg.answer)[0]


async def test_repeat_without_draft_is_handled(state, ctx, db):
    cb = make_callback()
    await h.on_repeat(cb, h.RepeatCb(value="none"), state, **_only(ctx, "db", "reminders", "tz"))
    assert db.list_pending(OWNER) == []
    cb.answer.assert_called()


# --- Список и карточка ------------------------------------------------------------


async def test_list_shows_only_pending_sorted(ctx, db, now):
    """S5."""
    later = db.add(OWNER, "позже", now + timedelta(days=1))
    sooner = db.add(OWNER, "раньше", now + timedelta(hours=1))
    done = db.add(OWNER, "готово", now + timedelta(hours=2))
    db.set_status(done, "done")
    msg = make_message(h.LIST_BUTTON)
    await h.show_list(msg, db=db, tz=TZ)
    kb = msg.answer.call_args.kwargs["reply_markup"].inline_keyboard
    labels = [row[0].text for row in kb]
    assert len(labels) == 2
    assert "раньше" in labels[0] and "позже" in labels[1]
    assert "08.11" in labels[0] and "13:00" in labels[0]
    assert h.ReminderCb.unpack(kb[0][0].callback_data).id == sooner
    assert later != sooner


async def test_list_marks_repeating(ctx, db, now):
    db.add(OWNER, "зарядка", now + timedelta(hours=1), Repeat.DAILY)
    msg = make_message(h.LIST_BUTTON)
    await h.show_list(msg, db=db, tz=TZ)
    assert "🔁" in msg.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text


async def test_empty_list(db):
    msg = make_message(h.LIST_BUTTON)
    await h.show_list(msg, db=db, tz=TZ)
    assert sent_texts(msg.answer) == ["Нет активных напоминаний."]


async def test_open_own_card_escaped(db, now):
    rid = db.add(OWNER, "<script>", now + timedelta(hours=1), Repeat.WEEKLY)
    cb = make_callback()
    await h.on_open(cb, h.ReminderCb(action="open", id=rid), db=db, tz=TZ)
    text = sent_texts(cb.message.edit_text)[0]
    assert "&lt;script&gt;" in text
    assert "08.11.2026 13:00" in text
    assert "еженедельно" in text


async def test_open_foreign_card_not_found(db, now):
    """A2."""
    rid = db.add(STRANGER, "чужое", now + timedelta(hours=1))
    cb = make_callback()
    await h.on_open(cb, h.ReminderCb(action="open", id=rid), db=db, tz=TZ)
    cb.message.edit_text.assert_not_called()
    assert "не найдено" in cb.answer.call_args.kwargs.get("text", cb.answer.call_args.args[0])


# --- Изменение времени (S6) ------------------------------------------------------


async def test_edit_time_flow(state, ctx, db, aps, now):
    rid = db.add(OWNER, "x", now + timedelta(hours=1))
    ctx["reminders"].schedule(db.get(rid, OWNER))
    await h.on_edit(make_callback(), h.ReminderCb(action="edit", id=rid), state, db=db)
    assert await state.get_state() == h.EditReminder.time.state

    await h.on_new_time(make_message("завтра 10:00"), state, **ctx)
    new = datetime(2026, 11, 9, 10, 0, tzinfo=TZ)
    assert db.get(rid, OWNER).remind_at == new
    assert aps.jobs[f"reminder-{rid}"]["run_date"] == new
    assert await state.get_state() is None


async def test_edit_foreign_refused(state, db, now):
    rid = db.add(STRANGER, "x", now + timedelta(hours=1))
    cb = make_callback()
    await h.on_edit(cb, h.ReminderCb(action="edit", id=rid), state, db=db)
    assert await state.get_state() is None
    cb.answer.assert_called()


async def test_new_time_for_foreign_id_does_nothing(state, ctx, db, now):
    """A2: даже если id чужого напоминания попал в FSM."""
    rid = db.add(STRANGER, "x", now + timedelta(hours=1))
    await state.set_state(h.EditReminder.time)
    await state.update_data(reminder_id=rid)
    msg = make_message("завтра 10:00")
    await h.on_new_time(msg, state, **ctx)
    assert db.get(rid, STRANGER).remind_at == now + timedelta(hours=1)
    assert "не найдено" in sent_texts(msg.answer)[0]


async def test_new_time_invalid_keeps_state(state, ctx, db, now):
    rid = db.add(OWNER, "x", now + timedelta(hours=1))
    await state.set_state(h.EditReminder.time)
    await state.update_data(reminder_id=rid)
    await h.on_new_time(make_message("ерунда"), state, **ctx)
    assert await state.get_state() == h.EditReminder.time.state


# --- Удаление (S6, A2, R4) --------------------------------------------------------


async def test_delete_own(ctx, db, aps, now):
    rid = db.add(OWNER, "x", now + timedelta(hours=1), Repeat.DAILY)
    ctx["reminders"].schedule(db.get(rid, OWNER))
    cb = make_callback()
    await h.on_delete(cb, h.ReminderCb(action="delete", id=rid), db=db, reminders=ctx["reminders"])
    assert db.get(rid, OWNER) is None
    assert f"reminder-{rid}" not in aps.jobs


async def test_delete_foreign_refused(ctx, db, aps, now):
    rid = db.add(STRANGER, "x", now + timedelta(hours=1))
    ctx["reminders"].schedule(db.get(rid, STRANGER))
    cb = make_callback()
    await h.on_delete(cb, h.ReminderCb(action="delete", id=rid), db=db, reminders=ctx["reminders"])
    assert db.get(rid, STRANGER) is not None
    assert f"reminder-{rid}" in aps.jobs


# --- Отложить / Готово (S7) -------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("m15", datetime(2026, 11, 8, 12, 15, tzinfo=TZ)),
        ("h1", datetime(2026, 11, 8, 13, 0, tzinfo=TZ)),
        ("tmr", datetime(2026, 11, 9, 9, 0, tzinfo=TZ)),
    ],
)
async def test_snooze_sent_reminder(ctx, db, aps, now, action, expected):
    rid = db.add(OWNER, "x", now - timedelta(minutes=1))
    db.set_status(rid, "sent")
    cb = make_callback()
    await h.on_snooze(cb, h.SnoozeCb(action=action, id=rid), **ctx)
    r = db.get(rid, OWNER)
    assert (r.status, r.remind_at) == ("pending", expected)
    assert aps.jobs[f"reminder-{rid}"]["run_date"] == expected
    assert "Отложено до" in sent_texts(cb.message.edit_text)[0]


async def test_snooze_pending_refused(ctx, db, now):
    """S7c: несработавшее не откладываем (старая кнопка)."""
    rid = db.add(OWNER, "x", now + timedelta(hours=3))
    cb = make_callback()
    await h.on_snooze(cb, h.SnoozeCb(action="m15", id=rid), **ctx)
    assert db.get(rid, OWNER).remind_at == now + timedelta(hours=3)


async def test_snooze_repeating_creates_one_off_copy(ctx, db, now):
    """S7c: серия не сдвигается, создаётся разовое напоминание."""
    series_time = now + timedelta(days=1)
    rid = db.add(OWNER, "зарядка", series_time, Repeat.DAILY)
    cb = make_callback()
    await h.on_snooze(cb, h.SnoozeCb(action="m15", id=rid), **ctx)
    assert db.get(rid, OWNER).remind_at == series_time
    copies = [r for r in db.list_pending(OWNER) if r.id != rid]
    assert len(copies) == 1
    assert (copies[0].task, copies[0].repeat) == ("зарядка", Repeat.NONE)
    assert copies[0].remind_at == now + timedelta(minutes=15)


async def test_done_then_done_again(ctx, db, now):
    """S7b."""
    rid = db.add(OWNER, "x", now - timedelta(minutes=1))
    db.set_status(rid, "sent")
    cb = make_callback()
    await h.on_snooze(cb, h.SnoozeCb(action="done", id=rid), **ctx)
    assert db.get(rid, OWNER).status == "done"
    cb.message.edit_reply_markup.assert_called_once()

    cb2 = make_callback()
    await h.on_snooze(cb2, h.SnoozeCb(action="done", id=rid), **ctx)
    assert db.get(rid, OWNER).status == "done"
    assert "уже" in str(cb2.answer.call_args).lower()


async def test_snooze_foreign_refused(ctx, db, now):
    rid = db.add(STRANGER, "x", now - timedelta(minutes=1))
    db.set_status(rid, "sent")
    cb = make_callback()
    await h.on_snooze(cb, h.SnoozeCb(action="m15", id=rid), **ctx)
    assert db.get(rid, STRANGER).status == "sent"


# --- /start, /cancel (O1) --------------------------------------------------------


async def test_start_shows_menu(state):
    msg = make_message("/start")
    await basic.cmd_start(msg, state)
    assert msg.answer.call_args.kwargs["reply_markup"] is not None


async def test_cancel_in_fsm(state):
    await state.set_state(h.CreateReminder.time)
    msg = make_message("/cancel")
    await basic.cmd_cancel(msg, state)
    assert await state.get_state() is None
    msg.answer.assert_called_once()


async def test_cancel_outside_fsm_silent(state):
    msg = make_message("/cancel")
    await basic.cmd_cancel(msg, state)
    msg.answer.assert_not_called()


def _only(d: dict, *keys):
    return {k: d[k] for k in keys}


async def test_stale_repeat_button_keeps_other_state(state):
    """Старая кнопка повтора во время изменения времени не сбрасывает сценарий."""
    await state.set_state(h.EditReminder.time)
    await state.update_data(reminder_id=5)
    cb = make_callback()
    await h.on_repeat_stale(cb)
    assert await state.get_state() == h.EditReminder.time.state
    assert (await state.get_data())["reminder_id"] == 5
    assert "устарела" in str(cb.answer.call_args)


async def test_snooze_on_inaccessible_message_sends_new_message(ctx, db, now):
    """Сообщение старше 48 ч (InaccessibleMessage): БД меняется, ответ — новым сообщением."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Chat, InaccessibleMessage

    rid = db.add(OWNER, "x", now - timedelta(days=3))
    db.set_status(rid, "sent")
    cb = MagicMock()
    cb.message = InaccessibleMessage(chat=Chat(id=OWNER, type="private"), message_id=1)
    cb.bot.send_message = AsyncMock()
    cb.answer = AsyncMock()
    await h.on_snooze(cb, h.SnoozeCb(action="h1", id=rid), **ctx)
    assert db.get(rid, OWNER).status == "pending"
    assert "Отложено до" in cb.bot.send_message.call_args.args[1]
    cb.answer.assert_called_once()


# --- Время из текста (docs/specs/text-time-extraction.md) ---------------------------


def _intent(result=None, error=None):
    from unittest.mock import AsyncMock, MagicMock

    intent = MagicMock()
    intent.parse = AsyncMock(return_value=result, side_effect=error)
    return intent


async def _free(msg, state, ctx, intent=None):
    await h.on_free_text(msg, state, intent=intent or _intent(), tz=TZ, clock=ctx["clock"])


async def test_free_text_with_time_asks_repeat_only(state, ctx, db, aps, now):
    """B1: время не спрашивается, сразу «Повторять?»; после «Один раз» — в БД и планировщике."""
    msg = make_message("через 2 минуты напомни про лимиты клода")
    intent = _intent()
    await _free(msg, state, ctx, intent)
    intent.parse.assert_not_called()
    assert await state.get_state() == h.CreateReminder.repeat.state
    text = sent_texts(msg.answer)[0]
    assert "«лимиты клода»" in text and "08.11.2026 12:02" in text and "Повторять" in text
    assert msg.answer.call_args.kwargs["reply_markup"] is not None

    cb = make_callback()
    await h.on_repeat(cb, h.RepeatCb(value="none"), state, **_only(ctx, "db", "reminders", "tz"))
    [r] = db.list_pending(OWNER)
    assert (r.task, r.remind_at) == ("лимиты клода", now + timedelta(minutes=2))
    assert f"reminder-{r.id}" in aps.jobs


async def test_new_task_with_time_skips_when_question(state, ctx, now):
    """B2."""
    await h.start_create(make_message(h.CREATE_BUTTON), state)
    msg = make_message("через 2 минуты напомни про лимиты клода")
    await h.on_task(msg, state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.repeat.state
    data = await state.get_data()
    assert data["task"] == "лимиты клода"
    assert datetime.fromisoformat(data["remind_at"]) == now + timedelta(minutes=2)
    assert "Когда" not in sent_texts(msg.answer)[0]


async def test_new_task_without_time_asks_when(state, ctx):
    """B3."""
    await state.set_state(h.CreateReminder.task)
    msg = make_message("купить хлеб")
    await h.on_task(msg, state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.time.state
    assert (await state.get_data())["task"] == "купить хлеб"
    assert "Когда" in sent_texts(msg.answer)[0]


async def test_time_without_task_then_task(state, ctx, now):
    """B4: «напомни через 5 минут» → «О чём?» → задача → повтор, время сохранено."""
    msg = make_message("напомни через 5 минут")
    await _free(msg, state, ctx)
    assert await state.get_state() == h.CreateReminder.task.state
    assert "О чём" in sent_texts(msg.answer)[0]

    msg2 = make_message("размяться")
    await h.on_task(msg2, state, tz=TZ, clock=ctx["clock"])
    assert await state.get_state() == h.CreateReminder.repeat.state
    data = await state.get_data()
    assert data["task"] == "размяться"
    assert datetime.fromisoformat(data["remind_at"]) == now + timedelta(minutes=5)


async def test_free_text_falls_back_to_gemini(state, ctx, now):
    """B5: regex не нашёл время → Gemini с подписью «Текстовая команда»."""
    from services.intent import ParsedReminder

    when = datetime(2026, 11, 13, 19, 0, tzinfo=TZ)
    intent = _intent(ParsedReminder("купить хлеб", when, Repeat.NONE))
    msg = make_message("напомни купить хлеб в пятницу вечером")
    await _free(msg, state, ctx, intent)
    args, kwargs = intent.parse.call_args
    assert args[0] == "напомни купить хлеб в пятницу вечером"
    assert kwargs["kind"] == "Текстовая команда"
    assert await state.get_state() == h.CreateReminder.repeat.state
    data = await state.get_data()
    assert (data["task"], datetime.fromisoformat(data["remind_at"])) == ("купить хлеб", when)
    status = msg.answer.return_value
    assert "Повторять" in sent_texts(status.edit_text)[-1]


async def test_free_text_gemini_error(state, ctx, db):
    """B6."""
    from services.intent import IntentError

    msg = make_message("напомни когда-нибудь купить хлеб")
    await _free(msg, state, ctx, _intent(error=IntentError("Не понял, когда напомнить.")))
    assert await state.get_state() is None
    assert db.list_pending(OWNER) == []
    text = sent_texts(msg.answer.return_value.edit_text)[-1]
    assert "/new" in text and "через 20 мин" in text


async def test_free_text_gemini_network_error(state, ctx):
    msg = make_message("напомни когда-нибудь купить хлеб")
    await _free(msg, state, ctx, _intent(error=RuntimeError("503")))
    assert await state.get_state() is None
    assert "/new" in sent_texts(msg.answer.return_value.edit_text)[-1]


async def test_free_text_past_time(state, ctx):
    """E8 в боте: понятная ошибка, состояние не меняется."""
    msg = make_message("сегодня 10:00 напомни выпить кофе")
    await _free(msg, state, ctx)
    assert await state.get_state() is None
    assert "прошл" in sent_texts(msg.answer)[0]


async def test_free_text_html_escaped(state, ctx):
    """B9."""
    msg = make_message("через 2 мин напомни про <b>")
    await _free(msg, state, ctx)
    assert "«&lt;b&gt;»" in sent_texts(msg.answer)[0]
