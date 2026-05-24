from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from croniter import croniter


def next_tick(cron_expr: str, after: datetime | None = None) -> datetime:
    base = after or datetime.now(tz=timezone.utc)
    it = croniter(cron_expr, base)
    return it.get_next(datetime)


async def sleep_until_next_tick(cron_expr: str) -> datetime:
    tick = next_tick(cron_expr)
    now = datetime.now(tz=timezone.utc)
    delay = (tick - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    return tick
