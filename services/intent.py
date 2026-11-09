"""Голосовая команда → {task, remind_at, repeat} через Gemini (structured output).

Временные ошибки Gemini (503/429/таймаут) повторяются встроенными ретраями SDK
(HttpRetryOptions), затем — одна попытка на запасной модели, затем LLMUnavailableError.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

import aiohttp
import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

from services.timeparse import Repeat, next_occurrence

_FMT = "%Y-%m-%d %H:%M"
log = logging.getLogger(__name__)

# Временные ошибки: перегрузка, лимит, таймаут — имеет смысл повторить.
TRANSIENT_CODES = (408, 429, 500, 502, 503, 504)
_TRANSIENT_EXC = (
    httpx.TimeoutException,
    httpx.ConnectError,
    TimeoutError,
    # на случай, если SDK всё же пойдёт через aiohttp
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
)

RetryHook = Callable[[str], Awaitable[None]]  # stage: "busy" | "fallback"


class IntentError(ValueError):
    """Понятная пользователю ошибка разбора."""


class LLMUnavailableError(Exception):
    """Gemini недоступен после всех попыток (503/429/таймаут/сеть)."""


def is_transient(err: BaseException) -> bool:
    if isinstance(err, errors.APIError):
        return err.code in TRANSIENT_CODES
    return isinstance(err, _TRANSIENT_EXC)


def _describe(err: BaseException) -> str:
    if isinstance(err, errors.APIError):
        return f"{err.code} {err.status or ''}".strip()
    return "таймаут" if isinstance(err, (httpx.TimeoutException, TimeoutError)) else "нет связи"


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
    def __init__(
        self,
        api_key: str,
        model: str,
        tz: tzinfo,
        client=None,
        *,
        fallback_model: str = "",
        timeout_s: float = 20,
        retry_attempts: int = 2,
        retry_initial_delay: float = 1.0,
        retry_max_delay: float = 8.0,
    ):
        self.api_key = api_key
        self.model = model
        self.tz = tz
        self._client = client
        self.fallback_model = fallback_model
        self.timeout_s = timeout_s
        self.retry_attempts = retry_attempts  # попытки встроенным ретраем после первой неудачи
        self.retry_initial_delay = retry_initial_delay
        self.retry_max_delay = retry_max_delay

    def _get_client(self):
        if self._client is None:
            # Явно httpx: иначе при установленном aiohttp (его тянет aiogram) SDK выбрал бы
            # aiohttp, а ретраи и тесты (httpx MockTransport) рассчитаны на httpx.
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(httpx_async_client=httpx.AsyncClient()),
            )
        return self._client

    def _config(self, now: datetime, attempts: int = 1) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=build_system_prompt(now),
            response_mime_type="application/json",
            response_schema=ReminderIntent,
            temperature=0,
            http_options=types.HttpOptions(
                timeout=int(self.timeout_s * 1000),
                retry_options=types.HttpRetryOptions(
                    attempts=attempts,
                    initial_delay=self.retry_initial_delay,
                    max_delay=self.retry_max_delay,
                    exp_base=2.0,
                    http_status_codes=list(TRANSIENT_CODES),
                ),
            ),
        )

    async def _call(self, model: str, user: str, now: datetime, attempts: int) -> str | None:
        resp = await self._get_client().aio.models.generate_content(
            model=model, contents=user, config=self._config(now, attempts)
        )
        return resp.text

    async def _generate(self, user: str, now: datetime, on_retry: RetryHook | None) -> str | None:
        """1 попытка → (busy) ещё retry_attempts встроенным ретраем → (fallback) запасная модель."""
        plan = [(self.model, 1, None), (self.model, self.retry_attempts, "busy")]
        if self.fallback_model and self.fallback_model != self.model:
            plan.append((self.fallback_model, 1, "fallback"))
        last: BaseException | None = None
        for model, attempts, stage in plan:
            if stage is not None and on_retry is not None:
                try:
                    await on_retry(stage)
                except Exception:  # сбой статуса в Telegram не должен отменять ретраи
                    log.warning("Retry status hook failed", exc_info=True)
            try:
                return await self._call(model, user, now, attempts)
            except Exception as err:
                if not is_transient(err):
                    raise
                log.warning("Gemini %s unavailable: %s", model, _describe(err))
                last = err
        raise LLMUnavailableError(_describe(last)) from last

    async def parse(
        self,
        text: str,
        context: str | None,
        now: datetime,
        kind: str = "Голосовая команда",
        on_retry: RetryHook | None = None,
    ) -> ParsedReminder:
        user = f"{kind}: {text}"
        if context:
            user = f"Контекст от пользователя: {context}\n{user}"
        raw = await self._generate(user, now, on_retry)
        if not raw:
            raise IntentError("ИИ вернул пустой ответ. Попробуй ещё раз.")
        try:
            data = ReminderIntent.model_validate_json(raw)
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
