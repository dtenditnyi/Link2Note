"""Голосовая команда → {task, remind_at, repeat} через Gemini (structured output)."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from services.timeparse import Repeat, next_occurrence

_FMT = "%Y-%m-%d %H:%M"


class IntentError(ValueError):
    """Понятная пользователю ошибка разбора."""


class ReminderIntent(BaseModel):
    """Схема ответа LLM. Без значений по умолчанию — Gemini их не поддерживает в схеме."""

    task: str = Field(
        description="Суть действия, без «напомни мне». Если «это» — подставь контекст"
    )
    remind_at: str = Field(description="Локальное время напоминания в формате YYYY-MM-DD HH:MM")
    repeat: Literal["none", "daily", "weekly"] = Field(
        description="none — один раз; daily — каждый день; weekly — каждую неделю"
    )


@dataclass(frozen=True)
class ParsedReminder:
    task: str
    remind_at: datetime  # aware
    repeat: Repeat


def build_system_prompt(now: datetime) -> str:
    """Примеры строятся от текущего времени, чтобы модель не тянулась к прошлым датам."""
    now_s = now.strftime(_FMT)
    in_two_min = (now + timedelta(minutes=2)).strftime(_FMT)
    days_to_sat = (5 - now.weekday()) % 7 or 7
    saturday = datetime.combine(now.date() + timedelta(days=days_to_sat), time(19, 0))
    saturday_s = saturday.strftime(_FMT)
    nine = datetime.combine(now.date(), time(9, 0))
    if nine <= now.replace(tzinfo=None):
        nine += timedelta(days=1)
    return f"""Ты — парсер напоминаний. Извлеки из сообщения пользователя напоминание.
Текущее время: {now_s} ({now.tzinfo}). Все времена — в этом часовом поясе.

Правила:
1. task — только суть действия, без слов-паразитов («напомни мне», «короче», «пожалуйста»).
2. Если есть «Контекст от пользователя» и команда говорит «это/его/по нему» —
   подставь контекст в task.
3. Относительное время («через 2 минуты», «завтра утром», «в субботу») считай от текущего времени.
   «Утром» = 09:00, «днём» = 13:00, «вечером» = 19:00, если час не назван.
4. remind_at — строго YYYY-MM-DD HH:MM и обязательно в будущем.
5. repeat: daily — «каждый день», weekly — «каждую неделю / по понедельникам», иначе none.

Примеры:
ВХОД: Голосовая команда: короче напомни мне выпить таблетки через две минуты
ВЫХОД: {{"task": "Выпить таблетки", "remind_at": "{in_two_min}", "repeat": "none"}}

ВХОД: Контекст от пользователя: https://eda.ru/recepty/pirog
Голосовая команда: напомни приготовить это в субботу вечером
ВЫХОД: {{"task": "Приготовить по рецепту: https://eda.ru/recepty/pirog",
        "remind_at": "{saturday_s}", "repeat": "none"}}

ВХОД: Голосовая команда: каждый день в девять утра делать зарядку
ВЫХОД: {{"task": "Сделать зарядку", "remind_at": "{nine.strftime(_FMT)}", "repeat": "daily"}}
"""


class IntentParser:
    def __init__(self, api_key: str, model: str, tz: tzinfo, client=None):
        self.api_key = api_key
        self.model = model
        self.tz = tz
        self._client = client

    def _get_client(self):
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _config(self, now: datetime) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=build_system_prompt(now),
            response_mime_type="application/json",
            response_schema=ReminderIntent,
            temperature=0,
        )

    async def parse(self, text: str, context: str | None, now: datetime) -> ParsedReminder:
        user = f"Голосовая команда: {text}"
        if context:
            user = f"Контекст от пользователя: {context}\n{user}"
        resp = await self._get_client().aio.models.generate_content(
            model=self.model, contents=user, config=self._config(now)
        )
        if not resp.text:
            raise IntentError("ИИ вернул пустой ответ. Попробуй ещё раз.")
        try:
            data = ReminderIntent.model_validate_json(resp.text)
        except ValidationError as err:
            raise IntentError("ИИ вернул некорректные данные. Попробуй ещё раз.") from err

        task = data.task.strip()
        if not task:
            raise IntentError("Не понял, о чём напомнить.")
        try:
            remind_at = datetime.strptime(data.remind_at.strip(), _FMT).replace(tzinfo=self.tz)
        except ValueError as err:
            raise IntentError("Не понял, когда напомнить.") from err

        repeat = Repeat(data.repeat)
        if remind_at <= now:
            if repeat is Repeat.NONE:
                raise IntentError("Получилось время в прошлом — скажи, когда напомнить.")
            remind_at = next_occurrence(remind_at, repeat, now, self.tz)
        return ParsedReminder(task, remind_at, repeat)
