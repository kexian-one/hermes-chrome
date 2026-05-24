"""Zombie oicc process cleanup — runs once at master startup."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


OICC_PORTS = range(18765, 18771)
_OICC_MARKER = "deploy\\oicc-b"


@dataclass
class _ZombieInfo:
    pid: int
    port: int
    cmdline: str


def _find_pids_on_ports() -> dict[int, int]:
    """Return {port: pid} for any process listening on the oicc port range."""
    ps_script = (
        "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue "
        "| Where-Object { $_.LocalPort -ge 18765 -and $_.LocalPort -le 18770 } "
        "| Select-Object LocalPort,OwningProcess "
        "| ConvertTo-Json -Depth 1"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        import json
        data = json.loads(result.stdout.strip())
        if isinstance(data, dict):
            data = [data]
        return {int(row["LocalPort"]): int(row["OwningProcess"]) for row in data if row.get("OwningProcess")}
    except Exception:
        return {}


def _get_process_cmdline(pid: int) -> str:
    ps_script = (
        f"(Get-WmiObject Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _kill_pid(pid: int) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
        capture_output=True,
        timeout=5,
    )


def kill_zombie_oicc_processes() -> list[_ZombieInfo]:
    """Kill leftover oicc node.exe processes from previous runs. Returns killed list."""
    port_to_pid = _find_pids_on_ports()
    if not port_to_pid:
        return []

    killed: list[_ZombieInfo] = []
    for port, pid in port_to_pid.items():
        cmdline = _get_process_cmdline(pid)
        if _OICC_MARKER.lower() in cmdline.lower():
            _kill_pid(pid)
            killed.append(_ZombieInfo(pid=pid, port=port, cmdline=cmdline))
            print(f"[zombie] killed pid={pid} port={port} cmd={cmdline[:120]}")
        else:
            print(f"[zombie] pid={pid} port={port} — not ours (cmd={cmdline[:80]!r}), skipped")

    return killed
