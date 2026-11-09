"""Ошибки конвейера конспектов. Текст сообщения показывается пользователю — пишем по-русски."""


class DigestError(Exception):
    """Понятная пользователю причина, почему конспект не получился."""


class VaultError(DigestError):
    """Заметку не удалось отправить в хранилище (она может остаться в локальном клоне)."""

    def __init__(self, message: str, *, note_name: str | None = None):
        super().__init__(message)
        self.note_name = note_name


class NoteNotFound(VaultError):
    """Заметки нет ни под своим именем, ни по url во frontmatter (удалена или ушла из хранилища)."""


class NoteAmbiguous(VaultError):
    """По url нашлось несколько заметок — не угадываем, какую менять. Это не «файла нет»."""


class VideoUnreadable(DigestError):
    """Gemini не смог открыть само видео (400/403/404, FAILED в Files API) — не перегрузка.

    Тогда есть смысл в запасном пути: звук → faster-whisper → конспект по тексту.
    """


class GeminiBusy(DigestError):
    """Основная и запасная модели перегружены (429/5xx) — задачу стоит отложить, а не ронять.

    daily=True — обе упёрлись в дневную квоту: ждать до её сброса, а не минуты.
    """

    def __init__(self, message: str, *, daily: bool = False):
        super().__init__(message)
        self.daily = daily
