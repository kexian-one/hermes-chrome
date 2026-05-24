from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.scheduler import next_tick


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_next_tick_basic():
    after = _utc(2026, 5, 23, 8, 0)
    tick = next_tick("0 9,15 * * *", after=after)
    assert tick == _utc(2026, 5, 23, 9, 0)


def test_next_tick_skips_to_afternoon():
    after = _utc(2026, 5, 23, 9, 1)
    tick = next_tick("0 9,15 * * *", after=after)
    assert tick == _utc(2026, 5, 23, 15, 0)


def test_next_tick_rolls_to_next_day():
    after = _utc(2026, 5, 23, 15, 1)
    tick = next_tick("0 9,15 * * *", after=after)
    assert tick == _utc(2026, 5, 24, 9, 0)


def test_next_tick_at_exact_boundary():
    after = _utc(2026, 5, 23, 9, 0)
    tick = next_tick("0 9,15 * * *", after=after)
    assert tick == _utc(2026, 5, 23, 15, 0)


def test_next_tick_hourly():
    after = _utc(2026, 5, 23, 10, 30)
    tick = next_tick("*/30 * * * *", after=after)
    assert tick == _utc(2026, 5, 23, 11, 0)


def test_next_tick_returns_future_datetime():
    tick = next_tick("0 9,15 * * *")
    assert tick > datetime.now(tz=timezone.utc)
