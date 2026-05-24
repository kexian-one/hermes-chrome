from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent.log_rotation import cleanup_old_logs


def _make_log(log_dir: Path, name: str, size_bytes: int, age_seconds: float) -> Path:
    f = log_dir / name
    f.write_bytes(b"x" * size_bytes)
    mtime = time.time() - age_seconds
    import os
    os.utime(f, (mtime, mtime))
    return f


def test_removes_old_logs(tmp_path: Path) -> None:
    old = _make_log(tmp_path, "worker-b1-old.log", 100, age_seconds=40 * 86400)
    recent = _make_log(tmp_path, "worker-b1-new.log", 100, age_seconds=1 * 86400)

    cleanup_old_logs(tmp_path, max_age_days=30, max_total_mb=1024)

    assert not old.exists()
    assert recent.exists()


def test_keeps_recent_logs(tmp_path: Path) -> None:
    f1 = _make_log(tmp_path, "worker-b1-ts1.log", 100, age_seconds=5 * 86400)
    f2 = _make_log(tmp_path, "worker-b2-ts2.log", 100, age_seconds=10 * 86400)

    cleanup_old_logs(tmp_path, max_age_days=30, max_total_mb=1024)

    assert f1.exists()
    assert f2.exists()


def test_removes_oldest_when_over_size_limit(tmp_path: Path) -> None:
    oldest = _make_log(tmp_path, "worker-b1-ts1.log", 600 * 1024 * 1024, age_seconds=2 * 86400)
    newer = _make_log(tmp_path, "worker-b1-ts2.log", 600 * 1024 * 1024, age_seconds=1 * 86400)

    cleanup_old_logs(tmp_path, max_age_days=30, max_total_mb=1000)

    assert not oldest.exists()
    assert newer.exists()


def test_noop_when_log_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-dir"
    cleanup_old_logs(missing, max_age_days=30, max_total_mb=1024)


def test_no_deletion_when_under_limits(tmp_path: Path) -> None:
    f = _make_log(tmp_path, "worker-b1-ts.log", 1024, age_seconds=1 * 86400)

    cleanup_old_logs(tmp_path, max_age_days=30, max_total_mb=1024)

    assert f.exists()
