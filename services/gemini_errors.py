"""Какие ошибки Gemini — «перегружен» (стоит попробовать запасную модель или позже)."""

from google.genai import errors


def is_overloaded(err: BaseException) -> bool:
    """429 (лимит) и любые 5xx — временные. 400/403/404 — нет: повтор не поможет."""
    return isinstance(err, errors.APIError) and (err.code == 429 or 500 <= err.code < 600)


def is_daily_quota(err: BaseException) -> bool:
    """429 по дневной квоте (quotaId …PerDay…): ждать минуты бессмысленно, сброс в полночь PT."""
    if not isinstance(err, errors.APIError) or err.code != 429:
        return False
    body = err.details if isinstance(err.details, dict) else {}
    for detail in (body.get("error") or {}).get("details") or []:
        for violation in detail.get("violations") or []:
            if "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False
