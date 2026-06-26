from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent.config import MasterConfig, WorkerConfig
from agent.mcp_client import resolve_node_bin
from agent.zombies import _find_pids_on_ports, _get_process_cmdline, _is_oicc_cmdline


@dataclass(frozen=True)
class BridgeWorkerStatus:
    worker_id: str
    port: int
    running: bool
    pid: int | None = None
    listening_pid: int | None = None
    reason: str = ""


def state_dir(project_root: Path) -> Path:
    return project_root / "state" / "oicc-bridge"


def supervisor_pidfile(project_root: Path) -> Path:
    return state_dir(project_root) / "supervisor.pid"


def worker_pidfile(project_root: Path, worker_id: str) -> Path:
    return state_dir(project_root) / f"{worker_id}.pid"


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_pid(pid: int, *, timeout: float = 6.0) -> None:
    if not _pid_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_running(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def status(config: MasterConfig) -> list[BridgeWorkerStatus]:
    port_to_pid = _find_pids_on_ports()
    rows: list[BridgeWorkerStatus] = []
    for wc in config.workers:
        pid = _read_pid(worker_pidfile(config.project_root, wc.worker_id))
        listening_pid = port_to_pid.get(wc.mcp_port)
        running = bool(pid and _pid_running(pid))
        reason = ""
        if listening_pid and not _is_oicc_cmdline(_get_process_cmdline(listening_pid)):
            reason = "port is occupied by a non-OICC process"
        elif not listening_pid:
            reason = "not listening"
        elif pid and listening_pid != pid:
            reason = "pidfile does not match listener"
        rows.append(BridgeWorkerStatus(
            worker_id=wc.worker_id,
            port=wc.mcp_port,
            running=running and listening_pid == pid,
            pid=pid if running else None,
            listening_pid=listening_pid,
            reason=reason,
        ))
    return rows


def supervisor_running(project_root: Path) -> bool:
    return _pid_running(_read_pid(supervisor_pidfile(project_root)))


def start_daemon(config: MasterConfig, *, python: str | None = None, wait_seconds: float = 10.0) -> int:
    pidfile = supervisor_pidfile(config.project_root)
    existing = _read_pid(pidfile)
    if _pid_running(existing):
        return int(existing)
    if existing:
        pidfile.unlink(missing_ok=True)

    log_path = config.log_dir / "oicc-bridge-supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        python or sys.executable,
        "-m",
        "scripts.oicc_bridge",
        "run",
    ]
    with log_path.open("ab") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(config.project_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + wait_seconds
    supervisor_pid: int | None = None
    while time.monotonic() < deadline:
        pid = _read_pid(pidfile)
        if _pid_running(pid):
            supervisor_pid = int(pid)
            if all(row.running for row in status(config)):
                return supervisor_pid
        if proc.poll() is not None:
            raise RuntimeError(f"oicc-bridge supervisor exited early with code {proc.returncode}")
        time.sleep(0.2)
    if supervisor_pid:
        return supervisor_pid
    raise TimeoutError("oicc-bridge supervisor did not write pidfile in time")


def stop_daemon(config: MasterConfig) -> None:
    pidfile = supervisor_pidfile(config.project_root)
    pid = _read_pid(pidfile)
    if pid:
        _terminate_pid(pid)
    pidfile.unlink(missing_ok=True)
    for wc in config.workers:
        child_pid = _read_pid(worker_pidfile(config.project_root, wc.worker_id))
        if child_pid:
            _terminate_pid(child_pid)
        worker_pidfile(config.project_root, wc.worker_id).unlink(missing_ok=True)


def _start_worker(config: MasterConfig, wc: WorkerConfig) -> subprocess.Popen:
    log_path = config.log_dir / f"oicc-bridge-{wc.worker_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["OICC_PORT"] = str(wc.mcp_port)
    with log_path.open("ab") as log_fh:
        log_fh.write(f"\n--- oicc-bridge {wc.worker_id} start port={wc.mcp_port} ---\n".encode("utf-8"))
        log_fh.flush()
        proc = subprocess.Popen(
            [resolve_node_bin("node"), str(wc.mcp_server_js_path)],
            cwd=str(wc.mcp_server_js_path.parent),
            env=env,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    _write_pid(worker_pidfile(config.project_root, wc.worker_id), proc.pid)
    return proc


def run_supervisor(config: MasterConfig) -> int:
    state_dir(config.project_root).mkdir(parents=True, exist_ok=True)
    pidfile = supervisor_pidfile(config.project_root)
    existing = _read_pid(pidfile)
    if existing and existing != os.getpid() and _pid_running(existing):
        print(f"[oicc-bridge] already running pid={existing}", flush=True)
        return 0
    _write_pid(pidfile, os.getpid())

    stopping = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    procs: dict[str, subprocess.Popen] = {}
    backoff_until: dict[str, float] = {}
    try:
        print("[oicc-bridge] supervisor started", flush=True)
        while not stopping:
            now = time.monotonic()
            for wc in config.workers:
                proc = procs.get(wc.worker_id)
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:
                    print(f"[oicc-bridge] {wc.worker_id} exited rc={proc.returncode}", flush=True)
                    worker_pidfile(config.project_root, wc.worker_id).unlink(missing_ok=True)
                    procs.pop(wc.worker_id, None)
                    backoff_until[wc.worker_id] = now + 3
                if now < backoff_until.get(wc.worker_id, 0):
                    continue
                if not wc.mcp_server_js_path.is_file():
                    print(f"[oicc-bridge] {wc.worker_id} missing {wc.mcp_server_js_path}", flush=True)
                    backoff_until[wc.worker_id] = now + 30
                    continue
                try:
                    procs[wc.worker_id] = _start_worker(config, wc)
                    print(
                        f"[oicc-bridge] {wc.worker_id} pid={procs[wc.worker_id].pid} port={wc.mcp_port}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[oicc-bridge] {wc.worker_id} start failed: {exc}", flush=True)
                    backoff_until[wc.worker_id] = now + 10
            time.sleep(1)
    finally:
        for worker_id, proc in list(procs.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            worker_pidfile(config.project_root, worker_id).unlink(missing_ok=True)
        pidfile.unlink(missing_ok=True)
        print("[oicc-bridge] supervisor stopped", flush=True)
    return 0
