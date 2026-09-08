from __future__ import annotations

import contextvars
import hashlib
import json
import os
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

deadline = contextvars.ContextVar("ecom_deadline", default=float("inf"))
cancel_token = contextvars.ContextVar("ecom_cancellation", default=None)
refresh_details = contextvars.ContextVar("ecom_refresh_details", default=False)


class OperationCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self, parent=None) -> None:
        self.event = threading.Event()
        self.parent = parent

    def is_set(self) -> bool:
        return self.event.is_set() or bool(self.parent and self.parent.is_set())

    def cancel(self) -> None:
        self.event.set()


@contextmanager
def cancellation_scope(timeout: float | None = None):
    scope = CancellationToken(cancel_token.get())
    cancel_context = cancel_token.set(scope)
    deadline_context = deadline.set(min(deadline.get(), time.monotonic() + timeout)) if timeout is not None else None
    try:
        yield scope
    finally:
        scope.cancel()
        cancel_token.reset(cancel_context)
        if deadline_context is not None:
            deadline.reset(deadline_context)


def remaining_timeout(default: float) -> float:
    token = cancel_token.get()
    if token is not None and token.is_set():
        raise OperationCancelled("任务已取消")
    remaining = deadline.get() - time.monotonic()
    if remaining <= 0 or default <= 0:
        raise TimeoutError("阶段预算已用尽")
    return min(default, remaining)


def cancellable_sleep(seconds: float) -> None:
    until = time.monotonic() + max(0, seconds)
    while time.monotonic() < until:
        left = until - time.monotonic()
        if left <= 0:
            break
        pause = remaining_timeout(min(0.05, left))
        token = cancel_token.get()
        if token is not None:
            token.event.wait(pause)
        else:
            time.sleep(pause)
    remaining_timeout(1)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def cache_key(operation: str, params: Any) -> str:
    raw = json.dumps([2, operation, params], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_cache(path: Path, ttl: float) -> Any:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and time.time() - float(cached.get("fetched_at", 0)) < ttl:
            return cached.get("payload")
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_cache(path: Path, payload: Any) -> None:
    atomic_json(path, {"fetched_at": time.time(), "payload": payload})


def parallel_map(items: list[Any], operation: Callable[[Any], Any], *, concurrency: int = 3, timeout: float = 120) -> list[dict[str, Any]]:
    if not items:
        return []
    phase_started = time.monotonic()
    expires = min(deadline.get(), time.monotonic() + timeout)
    stage_cancel = CancellationToken(cancel_token.get())
    def run(item: Any) -> dict[str, Any]:
        token = deadline.set(expires)
        cancellation = cancel_token.set(stage_cancel)
        started = time.monotonic()
        try:
            remaining_timeout(timeout)
            value = operation(item)
            remaining_timeout(timeout)
            return {"ok": True, "value": value, "elapsed_seconds": round(time.monotonic() - started, 3)}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "elapsed_seconds": round(time.monotonic() - started, 3)}
        finally:
            cancel_token.reset(cancellation)
            deadline.reset(token)
    pool = ThreadPoolExecutor(max_workers=max(1, min(concurrency, 8)), thread_name_prefix="ecom")
    futures = {pool.submit(contextvars.copy_context().run, run, item): index for index, item in enumerate(items)}
    results: list[dict[str, Any] | None] = [None] * len(items)
    pending = set(futures)
    try:
        while pending and not stage_cancel.is_set():
            remaining = expires - time.monotonic()
            if remaining <= 0:
                break
            completed, pending = wait(pending, timeout=min(0.05, remaining), return_when=FIRST_COMPLETED)
            for future in completed:
                results[futures[future]] = future.result()
        for future in list(pending):
            if future.done():
                results[futures[future]] = future.result()
                pending.remove(future)
        error = "OperationCancelled" if stage_cancel.is_set() else "TimeoutError"
        elapsed = round(time.monotonic() - phase_started, 3)
        return [value if value is not None else {"ok": False, "error": error, "elapsed_seconds": elapsed} for value in results]
    finally:
        stage_cancel.cancel()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
