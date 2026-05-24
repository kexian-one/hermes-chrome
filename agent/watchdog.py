"""Per-worker idle-timeout watchdog + slider escalation alert (Tasks #3 and #8)."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


_IDLE_CHECK_INTERVAL_SECS = 60
_DEFAULT_MAX_IDLE_MINUTES = 10

_SLIDER_RE = re.compile(r"滑块|slider|CAPTCHA|verify", re.IGNORECASE)
_SLIDER_QUIET_SECS = 30
_SLIDER_COOLDOWN_SECS = 300
_SLIDER_POLL_SECS = 5


class BotChannel(Protocol):
    async def send(self, chat_id: str, payload: dict) -> None: ...


async def idle_watchdog(
    proc: asyncio.subprocess.Process,
    worker_id: str,
    log_path: Path,
    max_idle_minutes: int = _DEFAULT_MAX_IDLE_MINUTES,
) -> None:
    """Kill proc if log_path goes unwritten for max_idle_minutes.

    Runs until proc exits. max_idle_minutes is read from Skill.max_idle_minutes
    by the caller (_max_idle_for_skill); default 10 when skill is unknown.
    """
    max_idle_secs = max_idle_minutes * 60
    while True:
        await asyncio.sleep(_IDLE_CHECK_INTERVAL_SECS)
        if proc.returncode is not None:
            return
        try:
            mtime = log_path.stat().st_mtime
            idle_secs = time.time() - mtime
        except FileNotFoundError:
            idle_secs = max_idle_secs + 1

        if idle_secs > max_idle_secs:
            print(
                f"[watchdog] {worker_id} idle {idle_secs:.0f}s > {max_idle_secs}s"
                f" — killing pid={proc.pid}",
                flush=True,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return


async def slider_watchdog(
    proc: asyncio.subprocess.Process,
    worker_id: str,
    log_path: Path,
    channel: BotChannel,
    chat_id: str,
    skill: str = "unknown",
) -> None:
    """Tail log for slider keywords; push a red alert card if quiet for 30s.

    Deduplicates: at most one alert per worker per 5 minutes.
    Runs until proc exits.
    """
    from agent.cards import error_card

    last_alert_ts: float = 0.0
    last_log_size: int = 0
    slider_detected_at: float | None = None

    while True:
        await asyncio.sleep(_SLIDER_POLL_SECS)
        if proc.returncode is not None:
            return

        try:
            stat = log_path.stat()
            new_size = stat.st_size
        except FileNotFoundError:
            continue

        if new_size <= last_log_size:
            if slider_detected_at is not None:
                quiet_secs = time.time() - slider_detected_at
                if quiet_secs >= _SLIDER_QUIET_SECS:
                    now = time.time()
                    if now - last_alert_ts >= _SLIDER_COOLDOWN_SECS:
                        last_alert_ts = now
                        slider_detected_at = None
                        card = error_card(
                            f"⚠ {skill} {worker_id} 卡在滑块 {_SLIDER_QUIET_SECS}s,需人工",
                            f"worker `{worker_id}` 运行 `{skill}` 时检测到滑块,"
                            f"已 {_SLIDER_QUIET_SECS}s 无新日志。请人工介入。",
                        )
                        try:
                            await channel.send(chat_id, {"card": card})
                        except Exception as exc:
                            print(f"[slider-alert] send failed: {exc}", flush=True)
            continue

        try:
            with log_path.open("rb") as f:
                f.seek(last_log_size)
                chunk = f.read(new_size - last_log_size).decode("utf-8", errors="replace")
        except OSError:
            last_log_size = new_size
            continue

        last_log_size = new_size

        if _SLIDER_RE.search(chunk):
            slider_detected_at = time.time()
        else:
            slider_detected_at = None
