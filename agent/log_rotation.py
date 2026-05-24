from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("log_rotation")


def cleanup_old_logs(
    log_dir: Path,
    max_age_days: int = 30,
    max_total_mb: int = 1024,
) -> None:
    if not log_dir.exists():
        return

    log_files = sorted(log_dir.glob("worker-*.log"), key=lambda p: p.stat().st_mtime)
    now = time.time()
    cutoff = now - max_age_days * 86400

    for f in list(log_files):
        if f.stat().st_mtime < cutoff:
            log.info("removing old log %s", f.name)
            f.unlink(missing_ok=True)
            log_files.remove(f)

    max_bytes = max_total_mb * 1024 * 1024
    total = sum(f.stat().st_size for f in log_files if f.exists())
    for f in list(log_files):
        if total <= max_bytes:
            break
        size = f.stat().st_size if f.exists() else 0
        log.info("removing oversized log %s (total %.1f MB)", f.name, total / 1024 / 1024)
        f.unlink(missing_ok=True)
        total -= size
