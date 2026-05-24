"""Browser kill / start / verify helpers.

Used by the ``restart_browser`` bot intent to do a controlled restart of a
specific Chromium-family browser (Chrome, Edge, Brave, Vivaldi, Opera).

Design notes:
- "Graceful close" via `CloseMainWindow` does NOT prevent session restore on
  Chromium browsers — closing the main window leaves renderer / GPU / utility
  child processes alive, requiring force-kill which marks the session as
  abnormal exit. We do graceful-then-force anyway as best effort.
- Tab restore is a real UX side effect: after restart, ALL previous tabs come
  back. Use the `cleanup-tabs` skill (see ``skills/cleanup-tabs/``) for tab
  cleanup; ``restart_browser`` is for memory / state resets.
- Cookies and login state survive restart (stored in User Data dir, not in
  process memory).
- After restart, launch with a warmup URL (default ``https://work.1688.com``)
  so the browser opens a page that matches the extension's host_permissions,
  waking the Manifest V3 service worker and triggering native-host spawn.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# Mapping from generic browser name → Windows process name (without .exe).
PROCESS_NAME_BY_BROWSER: dict[str, str] = {
    "chrome": "chrome",
    "edge": "msedge",
    "brave": "brave",
    "vivaldi": "vivaldi",
    "opera": "opera",
}


@dataclass(frozen=True)
class BrowserSpec:
    """How to launch + identify a specific browser.

    `name` is the human-friendly identifier (``chrome``, ``edge``, ...).
    `executable` is the full path to the browser .exe — used both for launch
    AND for surgical kill (only processes whose `.Path -eq executable` are
    killed, so Edge stable and Edge Dev / Beta are NOT mixed up since they
    install under different paths).
    `warmup_url` is opened on restart to force-activate the extension service
    worker.
    """
    name: str
    executable: Path
    warmup_url: str = "https://work.1688.com"


def _process_name(browser: str) -> str:
    key = browser.lower().strip()
    if key not in PROCESS_NAME_BY_BROWSER:
        raise ValueError(f"unknown browser name: {browser!r}")
    return PROCESS_NAME_BY_BROWSER[key]


def count_processes(browser: str) -> int:
    """How many OS processes match the browser. Best-effort, Windows-only."""
    proc_name = _process_name(browser)
    try:
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
    except (subprocess.TimeoutExpired, ValueError):
        return -1


def graceful_close(browser: str) -> int:
    """Try to send WM_CLOSE to all main windows of the browser.

    Returns the count of windows that accepted the close (not the count of
    processes — child processes don't respond to WM_CLOSE).
    """
    proc_name = _process_name(browser)
    try:
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
    except (subprocess.TimeoutExpired, ValueError):
        return -1


def force_kill_by_executable(executable: Path) -> int:
    """Force-kill ONLY processes whose `.Path` exactly equals the given exe.

    Surgical: Edge stable and Edge Dev install under different paths, so this
    won't mix them up. The user's own Edge windows DO get killed (acceptable
    per design: "我可以接受本人正在使用的浏览器被你杀掉"), but Edge Dev /
    Beta / Canary stay alive.
    """
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
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return int(result.stdout.strip() or "0")
    except (subprocess.TimeoutExpired, ValueError):
        return -1


def force_kill(browser: str) -> int:
    """⚠ DEPRECATED — kills all chrome/edge by name. Use force_kill_by_executable
    instead. Kept only for backward-compat with old tests."""
    proc_name = _process_name(browser)
    before = count_processes(browser)
    if before <= 0:
        return 0
    try:
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
    except subprocess.TimeoutExpired:
        return -1
    time.sleep(2)
    after = count_processes(browser)
    return max(before - max(after, 0), 0)


def launch(spec: BrowserSpec) -> bool:
    """Launch the browser detached with the warmup URL.

    Uses the user's default profile (login state + cookies preserved).
    Adds `--start-maximized` so the relaunched window fills the screen —
    without it, Chrome/Edge pick a default ~1024px-wide window that looks
    like "page rendered in left half of screen" on high-res displays.
    Returns True if Popen succeeded.
    """
    if not spec.executable.is_file():
        return False
    args = [str(spec.executable), "--start-maximized", spec.warmup_url]
    try:
        subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )
        return True
    except OSError:
        return False


@dataclass
class RestartResult:
    browser: str
    graceful_window_count: int
    force_killed: int
    launch_ok: bool
    bridge_ok: bool | None  # None = not probed
    elapsed_secs: float
    reason: str = ""


async def restart(
    spec: BrowserSpec,
    graceful_wait_secs: float = 8.0,
    post_launch_wait_secs: float = 25.0,
) -> RestartResult:
    """Full restart sequence: graceful close → force-kill by exe path → launch.

    Kills only processes whose .Path matches spec.executable, so Edge stable
    and Edge Dev (different install paths) are NOT mixed up.

    Does NOT verify the bridge — caller does that via `verify_bridge_alive`
    after this returns.
    """
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
