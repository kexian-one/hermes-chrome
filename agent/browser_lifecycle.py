"""Browser kill / start / verify helpers.

Used by the ``restart_browser`` bot intent to do a controlled restart of a
specific Chromium-family browser (Chrome, Edge, Brave, Vivaldi, Opera).

Windows and macOS differ in how browsers are registered, launched, and killed:
Windows uses PowerShell process metadata; macOS uses AppleScript for graceful
quit and POSIX process inspection for surgical force-kill. The public functions
below keep the same contract on both systems.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROCESS_NAME_BY_BROWSER: dict[str, str] = {
    "chrome": "chrome",
    "chrome-beta": "chrome",
    "chrome-canary": "chrome",
    "edge": "msedge",
    "edge-beta": "msedge",
    "edge-canary": "msedge",
    "edge-dev": "msedge",
    "brave": "brave",
    "vivaldi": "vivaldi",
    "opera": "opera",
    "chromium": "chromium",
}

MAC_APP_BY_BROWSER: dict[str, str] = {
    "chrome": "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "chrome-canary": "Google Chrome Canary",
    "edge": "Microsoft Edge",
    "edge-beta": "Microsoft Edge Beta",
    "edge-canary": "Microsoft Edge Canary",
    "edge-dev": "Microsoft Edge Dev",
    "brave": "Brave Browser",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "chromium": "Chromium",
}


@dataclass(frozen=True)
class BrowserSpec:
    """How to launch + identify a specific browser.

    `name` is the human-friendly identifier (``chrome``, ``edge``, ...).
    `executable` is the full browser path. On Windows this is the ``.exe``;
    on macOS it can be either ``/Applications/Google Chrome.app`` or the binary
    inside ``Contents/MacOS``. `warmup_url` is opened after restart to activate
    the extension service worker.
    """

    name: str
    executable: Path
    warmup_url: str = "https://work.1688.com"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _process_name(browser: str) -> str:
    key = browser.lower().strip()
    if key not in PROCESS_NAME_BY_BROWSER:
        raise ValueError(f"unknown browser name: {browser!r}")
    return PROCESS_NAME_BY_BROWSER[key]


def _mac_app_name(browser: str) -> str:
    key = browser.lower().strip()
    if key not in MAC_APP_BY_BROWSER:
        raise ValueError(f"unknown browser name: {browser!r}")
    return MAC_APP_BY_BROWSER[key]


def _count_processes_windows(browser: str) -> int:
    proc_name = _process_name(browser)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue | Measure-Object).Count",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return int(result.stdout.strip() or "0")


def _count_processes_posix(browser: str) -> int:
    name = _mac_app_name(browser) if _is_macos() else _process_name(browser)
    result = subprocess.run(
        ["pgrep", "-x", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode not in (0, 1):
        return -1
    if not result.stdout.strip():
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def count_processes(browser: str) -> int:
    """How many OS processes match the browser. Best-effort."""
    try:
        if _is_windows():
            return _count_processes_windows(browser)
        return _count_processes_posix(browser)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return -1


def _graceful_close_windows(browser: str) -> int:
    proc_name = _process_name(browser)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$ps = Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue "
            f"| Where-Object {{ $_.MainWindowHandle -ne 0 }}; "
            f"if ($ps) {{ ($ps | ForEach-Object {{ $_.CloseMainWindow() }} "
            f"| Where-Object {{ $_ }} | Measure-Object).Count }} else {{ 0 }}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return int(result.stdout.strip() or "0")


def _graceful_close_macos(browser: str) -> int:
    app_name = _mac_app_name(browser).replace('"', '\\"')
    result = subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to quit'],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return 1 if result.returncode == 0 else 0


def graceful_close(browser: str) -> int:
    """Try to ask the browser to quit gracefully."""
    try:
        if _is_windows():
            return _graceful_close_windows(browser)
        if _is_macos():
            return _graceful_close_macos(browser)
        return 0
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return -1


def _force_kill_by_executable_windows(executable: Path) -> int:
    exe = str(executable).replace("'", "''")
    ps_cmd = (
        "$procs = Get-Process | Where-Object { "
        f"$_.Path -eq '{exe}' "
        "}; "
        "if ($procs) { "
        "  $count = ($procs | Measure-Object).Count; "
        "  $procs | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }; "
        "  $count "
        "} else { 0 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return int(result.stdout.strip() or "0")


def _path_matches_executable(process_path: str, executable: Path) -> bool:
    if not process_path.strip():
        return False
    try:
        proc = Path(process_path).expanduser().resolve()
        target = executable.expanduser().resolve()
    except OSError:
        return False
    if target.suffix == ".app":
        return proc == target or target in proc.parents
    return proc == target


def _posix_processes_matching_executable(executable: Path) -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,comm="],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if _path_matches_executable(command.strip(), executable):
            pids.append(pid)
    return pids


def _force_kill_by_executable_posix(executable: Path) -> int:
    killed = 0
    for pid in _posix_processes_matching_executable(executable):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            killed += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
    return killed


def force_kill_by_executable(executable: Path) -> int:
    """Force-kill only processes whose executable path matches `executable`."""
    try:
        if _is_windows():
            return _force_kill_by_executable_windows(executable)
        return _force_kill_by_executable_posix(executable)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return -1


def force_kill(browser: str) -> int:
    """Deprecated: kills by process name. Use force_kill_by_executable instead."""
    before = count_processes(browser)
    if before <= 0:
        return 0
    try:
        if _is_windows():
            proc_name = _process_name(browser)
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue | Stop-Process -Force",
                ],
                capture_output=True,
                timeout=15,
            )
        else:
            name = _mac_app_name(browser) if _is_macos() else _process_name(browser)
            subprocess.run(["pkill", "-x", name], capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return -1
    time.sleep(2)
    after = count_processes(browser)
    return max(before - max(after, 0), 0)


def _launch_windows(spec: BrowserSpec) -> bool:
    args = [str(spec.executable), "--start-maximized", spec.warmup_url]
    subprocess.Popen(
        args,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
    return True


def _launch_posix(spec: BrowserSpec) -> bool:
    if _is_macos() and spec.executable.suffix == ".app":
        args = ["open", "-n", str(spec.executable), "--args", "--start-maximized", spec.warmup_url]
    else:
        args = [str(spec.executable), "--start-maximized", spec.warmup_url]
    subprocess.Popen(args, close_fds=True, start_new_session=True)
    return True


def launch(spec: BrowserSpec) -> bool:
    """Launch the browser detached with the warmup URL."""
    executable = spec.executable.expanduser()
    if not executable.exists():
        return False
    normalized = BrowserSpec(name=spec.name, executable=executable, warmup_url=spec.warmup_url)
    try:
        if _is_windows():
            return _launch_windows(normalized)
        return _launch_posix(normalized)
    except OSError:
        return False


@dataclass
class RestartResult:
    browser: str
    graceful_window_count: int
    force_killed: int
    launch_ok: bool
    bridge_ok: bool | None
    elapsed_secs: float
    reason: str = ""


async def restart(
    spec: BrowserSpec,
    graceful_wait_secs: float = 8.0,
    post_launch_wait_secs: float = 25.0,
) -> RestartResult:
    """Full restart sequence: graceful close → force-kill by executable → launch."""
    started = time.monotonic()

    graceful = graceful_close(spec.name)
    if graceful_wait_secs > 0:
        await asyncio.sleep(graceful_wait_secs)

    killed = force_kill_by_executable(spec.executable)

    launched = launch(spec)
    if launched and post_launch_wait_secs > 0:
        await asyncio.sleep(post_launch_wait_secs)

    elapsed = time.monotonic() - started
    return RestartResult(
        browser=spec.name,
        graceful_window_count=graceful,
        force_killed=killed,
        launch_ok=launched,
        bridge_ok=None,
        elapsed_secs=elapsed,
        reason="" if launched else "launch_failed",
    )
