"""Ошибки конвейера конспектов. Текст сообщения показывается пользователю — пишем по-русски."""


class DigestError(Exception):
    """Понятная пользователю причина, почему конспект не получился."""


class VaultError(DigestError):
    """Заметку не удалось отправить в хранилище (она может остаться в локальном клоне)."""

    def __init__(self, message: str, *, note_name: str | None = None):
        super().__init__(message)
        self.note_name = note_name


class GeminiBusy(DigestError):
    """Основная и запасная модели перегружены (429/5xx) — задачу стоит отложить, а не ронять.

    daily=True — обе упёрлись в дневную квоту: ждать до её сброса, а не минуты.
    """

    def __init__(self, message: str, *, daily: bool = False):
        super().__init__(message)
        self.daily = daily
