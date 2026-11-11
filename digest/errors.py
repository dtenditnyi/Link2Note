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


class VaultUnavailable(VaultError):
    """Хранилище временно недоступно (сеть, таймаут, 5xx, 429) — операцию стоит повторить.

    retry_after — сколько секунд просит подождать сервер (заголовок Retry-After), если сказал.
    """

    def __init__(
        self, message: str, *, retry_after: float | None = None, note_name: str | None = None
    ):
        super().__init__(message, note_name=note_name)
        self.retry_after = retry_after


class VaultRejected(VaultError):
    """Хранилище отказало по правилам или правам (400/401/403/409/422) — повтор не поможет."""


class VaultQueued(VaultError):
    """Сайт недоступен: операция лежит в очереди (outbox) и уйдёт сама.

    note_name — имя заметки (для save — желаемое; сайт может добавить « (2)»), op_id — строка
    vault_ops, к которой можно привязать сообщение Telegram для обновления после отправки.
    """

    def __init__(self, message: str, *, op_id: int, note_name: str | None = None):
        super().__init__(message, note_name=note_name)
        self.op_id = op_id
