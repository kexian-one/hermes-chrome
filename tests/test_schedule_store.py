from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.schedule_store import ScheduleStore


def test_empty_store(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    assert store.list_all() == []


def test_add_entry(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    entry = store.add(cron="0 16 * * *", worker="b2", skill="fapiao-1688", created_by="ou_x")
    assert entry.id == 1
    assert entry.cron == "0 16 * * *"
    assert entry.worker == "b2"
    assert entry.enabled is True


def test_add_increments_id(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    e1 = store.add("0 9 * * *", "b1", "fapiao-1688")
    e2 = store.add("0 10 * * *", "b2", "fapiao-1688")
    assert e1.id == 1 and e2.id == 2


def test_add_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "schedule.yaml"
    store = ScheduleStore(path)
    store.add("0 16 * * *", "b2", "fapiao-1688")
    assert path.is_file()

    fresh = ScheduleStore(path)
    entries = fresh.list_all()
    assert len(entries) == 1
    assert entries[0].worker == "b2"


def test_add_rejects_invalid_cron(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    with pytest.raises(ValueError):
        store.add("garbage cron", "b2", "fapiao-1688")


def test_remove_entry(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("0 9 * * *", "b1", "fapiao-1688")
    e2 = store.add("0 10 * * *", "b2", "fapiao-1688")

    assert store.remove(e2.id) is True
    assert len(store.list_all()) == 1


def test_remove_missing_id(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    assert store.remove(999) is False


def test_set_enabled(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    entry = store.add("0 9 * * *", "b1", "fapiao-1688")

    assert store.set_enabled(entry.id, False) is True
    assert store.list_all()[0].enabled is False

    # idempotent: same value returns False (no change)
    assert store.set_enabled(entry.id, False) is False


def test_find_due_within_window(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b1", "fapiao-1688")
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(minutes=1, seconds=5)
    due = store.find_due(now, later)
    assert len(due) == 1


def test_find_due_skips_disabled(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    entry = store.add("* * * * *", "b1", "fapiao-1688")
    store.set_enabled(entry.id, False)
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(minutes=5)
    assert store.find_due(now, later) == []


def test_find_due_misses_outside_window(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("0 16 * * *", "b1", "fapiao-1688")  # fires at 16:00
    now = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)  # 12:00
    later = now + timedelta(minutes=1)  # 12:01
    assert store.find_due(now, later) == []


def test_persisted_entry_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "schedule.yaml"
    s1 = ScheduleStore(path)
    s1.add("0 16 * * *", "b3", "fapiao-jd", created_by="ou_someone")
    s1.add("30 9 * * 1-5", "b1", "fapiao-1688-chase")

    s2 = ScheduleStore(path)
    entries = s2.list_all()
    assert len(entries) == 2
    assert entries[0].worker == "b3"
    assert entries[0].skill == "fapiao-jd"
    assert entries[1].cron == "30 9 * * 1-5"
