"""Tests for idle watchdog, slider escalation, zombie cleanup, and health check."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import LLMSettings, WorkerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test", api_key="test")


def _make_wc(tmp_path: Path, worker_id: str = "b1") -> WorkerConfig:
    b = int(worker_id[1:])
    return WorkerConfig(
        worker_id=worker_id,
        mcp_port=18764 + b,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=tmp_path,
        mcp_server_js_path=tmp_path / f"oicc-{worker_id}" / "host" / "mcp-server.js",
    )


class _FakeProc:
    """Fake asyncio.Process — starts alive, exits after a delay or on kill()."""

    def __init__(self, pid: int = 999, exit_code: int = 0, exit_after: float = 0.0):
        self.pid = pid
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._exit_after = exit_after
        self._done = asyncio.Event()

    async def wait(self) -> int:
        if self._exit_after > 0:
            await asyncio.sleep(self._exit_after)
        self.returncode = self._exit_code
        self._done.set()
        return self._exit_code

    def kill(self) -> None:
        self.returncode = -9
        self._done.set()


# ---------------------------------------------------------------------------
# Task #3 — idle watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_watchdog_kills_on_stale_log(tmp_path: Path):
    """Watchdog kills proc when log mtime is older than max_idle_minutes."""
    from agent.watchdog import idle_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_text("start\n")

    # Backdate the mtime so the file looks idle for > max_idle_minutes.
    stale_mtime = time.time() - 700  # 700s > 10 min
    import os
    os.utime(log_path, (stale_mtime, stale_mtime))

    proc = _FakeProc(pid=101)

    with patch("agent.watchdog._IDLE_CHECK_INTERVAL_SECS", 0.05):
        await asyncio.wait_for(idle_watchdog(proc, "b1", log_path, max_idle_minutes=10), timeout=2)

    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_idle_watchdog_exits_cleanly_when_proc_done(tmp_path: Path):
    """Watchdog stops by itself when the process has already exited."""
    from agent.watchdog import idle_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_text("done\n")

    proc = _FakeProc(pid=102)
    proc.returncode = 0  # already exited

    with patch("agent.watchdog._IDLE_CHECK_INTERVAL_SECS", 0.05):
        await asyncio.wait_for(idle_watchdog(proc, "b1", log_path, max_idle_minutes=10), timeout=2)

    # Should have returned cleanly (proc.kill() was not called).
    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_idle_watchdog_no_kill_if_log_updated(tmp_path: Path):
    """Watchdog does NOT kill when log is fresh."""
    from agent.watchdog import idle_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_text("start\n")

    proc = _FakeProc(pid=103, exit_after=0.15)
    # Log is fresh (mtime = now).

    with patch("agent.watchdog._IDLE_CHECK_INTERVAL_SECS", 0.05):
        await asyncio.wait_for(
            asyncio.gather(idle_watchdog(proc, "b1", log_path, max_idle_minutes=10), proc.wait()),
            timeout=2,
        )

    assert proc.returncode == 0


@pytest.mark.asyncio
async def test_idle_watchdog_missing_log_triggers_kill(tmp_path: Path):
    """Watchdog kills when log file does not exist (treat as idle)."""
    from agent.watchdog import idle_watchdog

    log_path = tmp_path / "nonexistent.log"  # does not exist
    proc = _FakeProc(pid=104)

    with patch("agent.watchdog._IDLE_CHECK_INTERVAL_SECS", 0.05):
        await asyncio.wait_for(idle_watchdog(proc, "b1", log_path, max_idle_minutes=0), timeout=2)

    assert proc.returncode == -9


# ---------------------------------------------------------------------------
# Task #8 — slider escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slider_watchdog_sends_alert(tmp_path: Path):
    """Alert is sent when slider keyword appears and log goes quiet for 30s."""
    from agent.watchdog import slider_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_bytes(b"")

    channel = MagicMock()
    channel.send = AsyncMock()

    proc = _FakeProc(pid=201)

    async def _feed_then_exit():
        await asyncio.sleep(0.1)
        log_path.write_text("step1\n滑块检测到\n", encoding="utf-8")
        # No more writes — simulate quiet period.
        await asyncio.sleep(2)
        proc.kill()

    with (
        patch("agent.watchdog._SLIDER_QUIET_SECS", 1),
        patch("agent.watchdog._SLIDER_COOLDOWN_SECS", 300),
        patch("agent.watchdog._SLIDER_POLL_SECS", 0.05),
    ):
        await asyncio.wait_for(
            asyncio.gather(
                slider_watchdog(proc, "b1", log_path, channel, "chat-xyz", "fapiao-1688"),
                _feed_then_exit(),
            ),
            timeout=5,
        )

    channel.send.assert_awaited_once()
    call_args = channel.send.call_args
    payload = call_args[0][1]
    assert "card" in payload


@pytest.mark.asyncio
async def test_slider_watchdog_no_alert_without_keyword(tmp_path: Path):
    """No alert when log is quiet but no slider keyword was detected."""
    from agent.watchdog import slider_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_bytes(b"normal output\n")

    channel = MagicMock()
    channel.send = AsyncMock()

    proc = _FakeProc(pid=202)

    async def _exit_soon():
        await asyncio.sleep(0.5)
        proc.kill()

    with (
        patch("agent.watchdog._SLIDER_QUIET_SECS", 0.1),
        patch("agent.watchdog._SLIDER_POLL_SECS", 0.05),
    ):
        await asyncio.wait_for(
            asyncio.gather(
                slider_watchdog(proc, "b1", log_path, channel, "chat-xyz", "fapiao-1688"),
                _exit_soon(),
            ),
            timeout=3,
        )

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_slider_watchdog_cooldown_deduplication(tmp_path: Path):
    """A second slider event within 5 min should NOT fire another alert."""
    from agent.watchdog import slider_watchdog

    log_path = tmp_path / "worker.log"
    log_path.write_bytes(b"")

    channel = MagicMock()
    channel.send = AsyncMock()

    proc = _FakeProc(pid=203)

    async def _two_sliders_then_exit():
        await asyncio.sleep(0.1)
        log_path.write_text("slider detected\n")
        await asyncio.sleep(1.5)  # quiet -> first alert fires
        log_path.write_text("slider detected\n")  # second event
        await asyncio.sleep(1.5)  # quiet -> should be suppressed
        proc.kill()

    with (
        patch("agent.watchdog._SLIDER_QUIET_SECS", 1),
        patch("agent.watchdog._SLIDER_COOLDOWN_SECS", 300),
        patch("agent.watchdog._SLIDER_POLL_SECS", 0.05),
    ):
        await asyncio.wait_for(
            asyncio.gather(
                slider_watchdog(proc, "b1", log_path, channel, "chat-xyz", "fapiao-1688"),
                _two_sliders_then_exit(),
            ),
            timeout=8,
        )

    assert channel.send.await_count == 1


# ---------------------------------------------------------------------------
# Task #9 — zombie cleanup
# ---------------------------------------------------------------------------


def test_kill_zombie_returns_empty_when_no_ports(monkeypatch):
    """When no processes listen on oicc ports, nothing is killed."""
    from agent.zombies import kill_zombie_oicc_processes

    with patch("agent.zombies._find_pids_on_ports", return_value={}):
        killed = kill_zombie_oicc_processes()

    assert killed == []


def test_kill_zombie_skips_non_oicc_process():
    """Processes with no oicc marker in cmdline are not killed."""
    from agent.zombies import kill_zombie_oicc_processes, _ZombieInfo

    killed_pids: list[int] = []

    def fake_kill(pid: int) -> None:
        killed_pids.append(pid)

    with (
        patch("agent.zombies._find_pids_on_ports", return_value={18765: 5000}),
        patch("agent.zombies._get_process_cmdline", return_value="node.exe some-other-app"),
        patch("agent.zombies._kill_pid", side_effect=fake_kill),
    ):
        killed = kill_zombie_oicc_processes()

    assert killed == []
    assert killed_pids == []


def test_kill_zombie_kills_oicc_process():
    """Processes with oicc marker in cmdline ARE killed."""
    from agent.zombies import kill_zombie_oicc_processes

    killed_pids: list[int] = []

    def fake_kill(pid: int) -> None:
        killed_pids.append(pid)

    oicc_cmd = r"node.exe C:\project\deploy\oicc-b2\host\mcp-server.js"

    with (
        patch("agent.zombies._find_pids_on_ports", return_value={18766: 6001}),
        patch("agent.zombies._get_process_cmdline", return_value=oicc_cmd),
        patch("agent.zombies._kill_pid", side_effect=fake_kill),
    ):
        killed = kill_zombie_oicc_processes()

    assert len(killed) == 1
    assert killed[0].pid == 6001
    assert killed[0].port == 18766
    assert 6001 in killed_pids


def test_kill_zombie_kills_multiple_ports():
    """Multiple oicc processes across different ports are all killed."""
    from agent.zombies import kill_zombie_oicc_processes

    killed_pids: list[int] = []

    def fake_kill(pid: int) -> None:
        killed_pids.append(pid)

    ports = {18765: 7001, 18767: 7003}
    cmd = r"node.exe C:\ai\all-in-ai\deploy\oicc-b1\host\mcp-server.js"

    with (
        patch("agent.zombies._find_pids_on_ports", return_value=ports),
        patch("agent.zombies._get_process_cmdline", return_value=cmd),
        patch("agent.zombies._kill_pid", side_effect=fake_kill),
    ):
        killed = kill_zombie_oicc_processes()

    assert len(killed) == 2
    assert set(killed_pids) == {7001, 7003}


def test_zombie_posix_lsof_parser():
    from agent.zombies import _find_pids_on_ports

    lsof = """COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
node    1234 user   24u  IPv4 0xabc      0t0  TCP 127.0.0.1:18765 (LISTEN)
node    5678 user   25u  IPv6 0xdef      0t0  TCP *:18770 (LISTEN)
node    9999 user   26u  IPv4 0xaaa      0t0  TCP 127.0.0.1:9999 (LISTEN)
"""
    with (
        patch("agent.zombies.sys.platform", "darwin"),
        patch("agent.zombies.subprocess.run", return_value=MagicMock(returncode=0, stdout=lsof)),
    ):
        assert _find_pids_on_ports() == {18765: 1234, 18770: 5678}


def test_zombie_marker_accepts_posix_paths():
    from agent.zombies import _is_oicc_cmdline

    assert _is_oicc_cmdline("node /Users/me/all-in-ai/deploy/oicc-b2/host/mcp-server.js")


# ---------------------------------------------------------------------------
# Task #10 — health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_all_healthy(tmp_path: Path):
    from agent.health import run_health_checks, log_health_results

    wcs = [_make_wc(tmp_path, f"b{i}") for i in range(1, 4)]

    async def _fake_probe(wc: WorkerConfig):
        from agent.health import WorkerHealth
        return WorkerHealth(worker_id=wc.worker_id, healthy=True)

    with patch("agent.health._probe_one", side_effect=_fake_probe):
        results = await run_health_checks(wcs)

    assert all(r.healthy for r in results)
    unhealthy = log_health_results(results)
    assert unhealthy == set()


@pytest.mark.asyncio
async def test_health_check_partial_failure(tmp_path: Path, capsys):
    from agent.health import run_health_checks, log_health_results

    wcs = [_make_wc(tmp_path, f"b{i}") for i in range(1, 4)]

    async def _fake_probe(wc: WorkerConfig):
        from agent.health import WorkerHealth
        if wc.worker_id == "b2":
            return WorkerHealth(worker_id="b2", healthy=False, reason="MCP unreachable")
        return WorkerHealth(worker_id=wc.worker_id, healthy=True)

    with patch("agent.health._probe_one", side_effect=_fake_probe):
        results = await run_health_checks(wcs)

    unhealthy = log_health_results(results)
    assert unhealthy == {"b2"}
    captured = capsys.readouterr()
    assert "b2=✗" in captured.out
    assert "b1=✓" in captured.out


@pytest.mark.asyncio
async def test_health_check_unhealthy_worker_skipped_in_fire(tmp_path: Path):
    """fire_due_entries skips workers marked unhealthy."""
    from agent.config import MasterConfig
    from agent.master import fire_due_entries
    from agent.schedule_store import ScheduleStore
    from datetime import datetime, timedelta, timezone

    workers = [_make_wc(tmp_path, f"b{i}") for i in range(1, 3)]
    config = MasterConfig(workers=workers, log_dir=tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b1", "fapiao-1688")
    store.add("* * * * *", "b2", "fapiao-1688")

    spawned: list[tuple] = []

    async def fake_create(*args, **kwargs):
        spawned.append(args)

        class _P:
            pid = 9999
            returncode: int | None = None

            async def wait(self):
                self.returncode = 0
                return 0

        return _P()

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
        results = await fire_due_entries(
            store, config, now, now + timedelta(minutes=1, seconds=5),
            unhealthy={"b2"},
        )

    worker_ids = {e.worker for e, _ in results}
    assert "b1" in worker_ids
    b2_codes = [code for e, code in results if e.worker == "b2"]
    assert b2_codes == [-2]
    assert len(spawned) == 1  # only b1 spawned


@pytest.mark.asyncio
async def test_health_restart_retries_probe(tmp_path: Path):
    """Restarting an unhealthy worker probes the extension; on success it
    clears the stale unhealthy mark and re-runs the worker's last skill."""
    from agent.config import MasterConfig
    from agent.master import _MasterDispatcherImpl
    from agent.schedule_store import ScheduleStore
    from agent.worker_state import WorkerStateTracker

    workers = [_make_wc(tmp_path, "b3")]
    config = MasterConfig(workers=workers, log_dir=tmp_path)
    store = ScheduleStore(tmp_path / "sched.yaml")
    tracker = WorkerStateTracker()
    # b3 has run a skill before (otherwise restart_worker refuses — no
    # "last task" to re-run).
    tracker.update_spawn("b3", "fapiao-1688", pid=1234)
    tracker.update_exit("b3", exit_code=0)
    unhealthy = {"b3"}

    spawned: list[tuple] = []

    async def fake_create(*args, **kwargs):
        spawned.append(args)

        class _P:
            pid = 8888
            returncode: int | None = None

            async def wait(self):
                self.returncode = 0
                return 0

        return _P()

    dispatcher = _MasterDispatcherImpl(config, tracker, store, [False], unhealthy=unhealthy)

    # probe_extension_connectivity is the new gate; patch it to "extension up"
    # so restart proceeds to spawn (instead of routing to browser restart).
    async def fake_probe_ext(worker_id):
        return True

    with (
        patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create),
        patch.object(dispatcher, "probe_extension_connectivity", side_effect=fake_probe_ext),
    ):
        await dispatcher.restart_worker("b3")
        await asyncio.sleep(0.05)

    assert "b3" not in unhealthy
    assert len(spawned) == 1
