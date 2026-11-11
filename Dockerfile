FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m botuser
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Сначала только зависимости — слой кешируется, пока не меняется uv.lock.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# Каталог кеша Whisper создаём заранее: иначе volume смонтируется от root и botuser не сможет писать.
RUN mkdir -p /app/data /home/botuser/.cache/huggingface \
    && chown -R botuser:botuser /app /home/botuser/.cache

USER botuser
# Бот раз в 30 с пишет data/heartbeat из своего event loop; завис loop — файл стареет → unhealthy.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["/app/.venv/bin/python", "-m", "services.heartbeat", "/app/data/heartbeat", "120"]
CMD ["/app/.venv/bin/python", "main.py"]
