from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from croniter import croniter


@dataclass
class ScheduleEntry:
    id: int
    cron: str
    worker: str
    skill: str
    enabled: bool = True
    created_by: str = ""
    created_at: str = ""
    # Origin tracking: which (channel, chat) created this schedule. Cron-fired
    # results are pushed back here so "the group that asked for the task gets
    # the result" — not the global alert sink. Empty = no human origin
    # (started via config or pre-existing yaml); falls back to alert sink.
    origin_app_id: str = ""
    origin_chat_id: str = ""

    def is_valid_cron(self) -> bool:
        try:
            croniter(self.cron)
            return True
        except Exception:
            return False


class ScheduleStore:
    """File-backed list of schedule entries persisted to YAML.

    Single-writer assumption: only the master process writes; bot intents call
    via the dispatcher. The bot's worker is in the same process so no IPC
    needed.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: list[ScheduleEntry] = []
        self._loaded = False

    def _load(self) -> None:
        if not self._path.is_file():
            self._entries = []
            self._loaded = True
            return
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if raw is None:
            self._entries = []
        elif isinstance(raw, list):
            self._entries = [self._coerce(item) for item in raw if isinstance(item, dict)]
        else:
            raise ValueError(f"{self._path}: top level must be a list")
        self._loaded = True

    @staticmethod
    def _coerce(d: dict) -> ScheduleEntry:
        return ScheduleEntry(
            id=int(d.get("id", 0)),
            cron=str(d.get("cron", "")),
            worker=str(d.get("worker", "")),
            skill=str(d.get("skill", "")),
            enabled=bool(d.get("enabled", True)),
            created_by=str(d.get("created_by", "")),
            created_at=str(d.get("created_at", "")),
            origin_app_id=str(d.get("origin_app_id", "")),
            origin_chat_id=str(d.get("origin_chat_id", "")),
        )

    def _save(self) -> None:
        """Atomic write: serialize to temp file in same dir, then os.replace().

        Without this, a crash mid-write leaves a truncated schedule.yaml and
        the user loses all schedule entries on restart.
        """
        import os
        import tempfile
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self._entries]
        payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        # NamedTemporaryFile in the same dir → os.replace is atomic on same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(self._path.parent),
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_name = tmp.name
        os.replace(tmp_name, self._path)

    def list_all(self) -> list[ScheduleEntry]:
        if not self._loaded:
            self._load()
        return list(self._entries)

    def reload(self) -> None:
        self._loaded = False
        self._load()

    def add(
        self,
        cron: str,
        worker: str,
        skill: str,
        created_by: str = "",
        origin_app_id: str = "",
        origin_chat_id: str = "",
    ) -> ScheduleEntry:
        if not self._loaded:
            self._load()
        next_id = max((e.id for e in self._entries), default=0) + 1
        entry = ScheduleEntry(
            id=next_id,
            cron=cron,
            worker=worker,
            skill=skill,
            enabled=True,
            created_by=created_by,
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            origin_app_id=origin_app_id,
            origin_chat_id=origin_chat_id,
        )
        if not entry.is_valid_cron():
            raise ValueError(f"invalid cron expression: {cron!r}")
        self._entries.append(entry)
        self._save()
        return entry

    def remove(self, entry_id: int) -> bool:
        if not self._loaded:
            self._load()
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        if len(self._entries) == before:
            return False
        self._save()
        return True

    def set_enabled(self, entry_id: int, enabled: bool) -> bool:
        if not self._loaded:
            self._load()
        for e in self._entries:
            if e.id == entry_id:
                if e.enabled == enabled:
                    return False
                e.enabled = enabled
                self._save()
                return True
        return False

    def find_due(self, window_start: datetime, window_end: datetime) -> list[ScheduleEntry]:
        """Return enabled entries whose cron fires strictly within (start, end]."""
        if not self._loaded:
            self._load()
        due: list[ScheduleEntry] = []
        for e in self._entries:
            if not e.enabled or not e.is_valid_cron():
                continue
            it = croniter(e.cron, window_start)
            next_fire = it.get_next(datetime)
            if window_start < next_fire <= window_end:
                due.append(e)
        return due
