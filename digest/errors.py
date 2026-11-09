"""Ошибки конвейера конспектов. Текст сообщения показывается пользователю — пишем по-русски."""


class DigestError(Exception):
    """Понятная пользователю причина, почему конспект не получился."""


class VaultError(DigestError):
    """Заметку не удалось отправить в хранилище (она может остаться в локальном клоне)."""

    def __init__(self, message: str, *, note_name: str | None = None):
        super().__init__(message)
        self.note_name = note_name
