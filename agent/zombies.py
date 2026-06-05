"""Zombie oicc process cleanup — runs once at master startup."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass


OICC_PORTS = range(18765, 18771)
_OICC_MARKERS = ("deploy\\oicc-b", "deploy/oicc-b")


@dataclass
class _ZombieInfo:
    pid: int
    port: int
    cmdline: str


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _find_pids_on_ports_windows() -> dict[int, int]:
    ps_script = (
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue "
        "| Where-Object { $_.LocalPort -ge 18765 -and $_.LocalPort -le 18770 } "
        "| Select-Object LocalPort,OwningProcess "
        "| ConvertTo-Json -Depth 1"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    data = json.loads(result.stdout.strip())
    if isinstance(data, dict):
        data = [data]
    return {
        int(row["LocalPort"]): int(row["OwningProcess"])
        for row in data
        if row.get("OwningProcess")
    }


def _find_pids_on_ports_posix() -> dict[int, int]:
    result = subprocess.run(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return {}
    port_to_pid: dict[int, int] = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        match = re.search(r":(\d+)(?:\s|\(|$)", parts[-2] if parts[-1] == "(LISTEN)" else parts[-1])
        if not match:
            match = re.search(r":(\d+)", line)
        if not match:
            continue
        port = int(match.group(1))
        if port in OICC_PORTS:
            port_to_pid[port] = pid
    return port_to_pid


def _find_pids_on_ports() -> dict[int, int]:
    """Return {port: pid} for any process listening on the oicc port range."""
    try:
        if _is_windows():
            return _find_pids_on_ports_windows()
        return _find_pids_on_ports_posix()
    except Exception:
        return {}


def _get_process_cmdline_windows(pid: int) -> str:
    ps_script = f"(Get-WmiObject Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return (result.stdout or "").strip()


def _get_process_cmdline_posix(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return (result.stdout or "").strip()


def _get_process_cmdline(pid: int) -> str:
    try:
        if _is_windows():
            return _get_process_cmdline_windows(pid)
        return _get_process_cmdline_posix(pid)
    except Exception:
        return ""


def _kill_pid_windows(pid: int) -> None:
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
        ],
        capture_output=True,
        timeout=5,
    )


def _kill_pid_posix(pid: int) -> None:
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        pass


def _kill_pid(pid: int) -> None:
    if _is_windows():
        _kill_pid_windows(pid)
    else:
        _kill_pid_posix(pid)


def _is_oicc_cmdline(cmdline: str) -> bool:
    normalized = cmdline.lower()
    return any(marker in normalized for marker in _OICC_MARKERS)


def kill_zombie_oicc_processes() -> list[_ZombieInfo]:
    """Kill leftover oicc node processes from previous runs. Returns killed list."""
    port_to_pid = _find_pids_on_ports()
    if not port_to_pid:
        return []

    killed: list[_ZombieInfo] = []
    for port, pid in port_to_pid.items():
        cmdline = _get_process_cmdline(pid)
        if _is_oicc_cmdline(cmdline):
            _kill_pid(pid)
            killed.append(_ZombieInfo(pid=pid, port=port, cmdline=cmdline))
            print(f"[zombie] killed pid={pid} port={port} cmd={cmdline[:120]}")
        else:
            print(f"[zombie] pid={pid} port={port} — not ours (cmd={cmdline[:80]!r}), skipped")

    return killed
