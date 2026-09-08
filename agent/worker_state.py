from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from threading import Lock
from pathlib import Path
import json
import os
import tempfile


@dataclass
class WorkerState:
    worker_id: str
    pid: int | None = None
    last_skill: str | None = None
    last_exit_code: int | None = None
    last_spawn: datetime | None = None
    last_finish: datetime | None = None
    alive: bool = False
    last_task: str = ""
    output_dir: str = ""


class WorkerStateTracker:
    def __init__(self, path: Path | None = None) -> None:
        self._states: dict[str, WorkerState] = {}
        self._lock = Lock()
        self._path = path
        if path and path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw:
                for key in ("last_spawn", "last_finish"):
                    if item.get(key):
                        item[key] = datetime.fromisoformat(item[key])
                item.update(alive=False, pid=None)
                state = WorkerState(**item)
                self._states[state.worker_id] = state

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump([asdict(s) for s in self._states.values()], stream, ensure_ascii=False, default=str)
            os.replace(temp, self._path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def _get_or_create(self, worker_id: str) -> WorkerState:
        if worker_id not in self._states:
            self._states[worker_id] = WorkerState(worker_id=worker_id)
        return self._states[worker_id]

    def remember_task(self, worker_id: str, task: str) -> None:
        with self._lock:
            self._get_or_create(worker_id).last_task = task
            self._save()

    def update_spawn(self, worker_id: str, skill: str, pid: int, task: str = "", output_dir: str = "") -> None:
        with self._lock:
            s = self._get_or_create(worker_id)
            s.pid = pid
            s.last_skill = skill
            s.last_spawn = datetime.now(tz=timezone.utc)
            s.last_finish = None
            s.alive = True
            if pid != 0:
                s.last_task = task
                s.output_dir = output_dir
            self._save()

    def update_exit(self, worker_id: str, exit_code: int) -> None:
        with self._lock:
            s = self._get_or_create(worker_id)
            s.last_exit_code = exit_code
            s.last_finish = datetime.now(tz=timezone.utc)
            s.alive = False
            s.pid = None
            self._save()

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
                    last_task=s.last_task,
                    output_dir=s.output_dir,
                )
                for s in self._states.values()
            ]
