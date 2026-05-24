from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock


@dataclass
class WorkerState:
    worker_id: str
    pid: int | None = None
    last_skill: str | None = None
    last_exit_code: int | None = None
    last_spawn: datetime | None = None
    last_finish: datetime | None = None
    alive: bool = False


class WorkerStateTracker:
    def __init__(self) -> None:
        self._states: dict[str, WorkerState] = {}
        self._lock = Lock()

    def _get_or_create(self, worker_id: str) -> WorkerState:
        if worker_id not in self._states:
            self._states[worker_id] = WorkerState(worker_id=worker_id)
        return self._states[worker_id]

    def update_spawn(self, worker_id: str, skill: str, pid: int) -> None:
        with self._lock:
            s = self._get_or_create(worker_id)
            s.pid = pid
            s.last_skill = skill
            s.last_spawn = datetime.now(tz=timezone.utc)
            s.last_finish = None
            s.alive = True

    def update_exit(self, worker_id: str, exit_code: int) -> None:
        with self._lock:
            s = self._get_or_create(worker_id)
            s.last_exit_code = exit_code
            s.last_finish = datetime.now(tz=timezone.utc)
            s.alive = False
            s.pid = None

    def snapshot(self) -> list[WorkerState]:
        with self._lock:
            return [
                WorkerState(
                    worker_id=s.worker_id,
                    pid=s.pid,
                    last_skill=s.last_skill,
                    last_exit_code=s.last_exit_code,
                    last_spawn=s.last_spawn,
                    last_finish=s.last_finish,
                    alive=s.alive,
                )
                for s in self._states.values()
            ]
