"""F2: heartbeat из event loop и проверка свежести для Docker HEALTHCHECK."""

import asyncio
from datetime import UTC, datetime, timedelta

from services.heartbeat import heartbeat_age, is_healthy, main, run_heartbeat, write_heartbeat

T = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def test_missing_or_garbage_is_unhealthy(tmp_path):
    hb = tmp_path / "hb"
    assert not is_healthy(hb, T)
    hb.write_text("мусор", encoding="utf-8")
    assert heartbeat_age(hb, T) is None and not is_healthy(hb, T)
    hb.write_text("2026-11-11T10:00:00", encoding="utf-8")  # без пояса
    assert not is_healthy(hb, T)


def test_age_boundaries(tmp_path):
    hb = tmp_path / "d" / "hb"
    write_heartbeat(hb, T)
    assert heartbeat_age(hb, T + timedelta(seconds=30)) == 30
    assert is_healthy(hb, T + timedelta(seconds=119))
    assert not is_healthy(hb, T + timedelta(seconds=120))
    assert not is_healthy(hb, T - timedelta(seconds=300))  # часы ушли назад сильно — подозрительно


async def test_loop_updates_file(tmp_path):
    hb = tmp_path / "hb"
    ticks = iter([T, T + timedelta(seconds=30), T + timedelta(seconds=60)])
    task = asyncio.create_task(run_heartbeat(hb, interval_s=0.01, clock=lambda: next(ticks, T)))
    await asyncio.sleep(0.1)
    task.cancel()
    assert hb.exists()


def test_cli_exit_codes(tmp_path, capsys):
    hb = tmp_path / "hb"
    assert main([str(hb)]) == 1
    write_heartbeat(hb, datetime.now(UTC))
    assert main([str(hb), "120"]) == 0
    assert "healthy" in capsys.readouterr().out
