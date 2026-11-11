"""B04: SIGTERM (docker stop) обрабатывается как Ctrl+C — штатная остановка, а не SIGKILL."""

import signal

import main


def test_run_handles_sigterm_like_ctrl_c(monkeypatch, caplog):
    old = signal.getsignal(signal.SIGTERM)
    seen = {}

    def fake_asyncio_run(coro):
        coro.close()
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        raise KeyboardInterrupt  # так приходит и SIGTERM после default_int_handler

    monkeypatch.setattr(main.asyncio, "run", fake_asyncio_run)
    try:
        caplog.set_level("INFO")
        main.run()
    finally:
        signal.signal(signal.SIGTERM, old)
    assert seen["handler"] is signal.default_int_handler
    assert "Бот остановлен" in caplog.text
