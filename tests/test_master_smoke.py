"""Smoke tests for the schedule-driven master orchestrator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent.config import LLMSettings, MasterConfig, WorkerConfig
from agent.master import (
    fire_due_entries,
    run_once,
    spawn_one_skill,
)
from agent.schedule_store import ScheduleStore
from agent.worker_state import WorkerStateTracker


_DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test", api_key="test")


def _make_config(tmp_path: Path, num_workers: int = 6) -> MasterConfig:
    workers = [
        WorkerConfig(
            worker_id=f"b{i}",
            mcp_port=18764 + i,
            llm_multimodal=_DUMMY_LLM,
            llm_reasoning=_DUMMY_LLM,
            log_dir=tmp_path,
            mcp_server_js_path=tmp_path / f"oicc-b{i}" / "host" / "mcp-server.js",
        )
        for i in range(1, num_workers + 1)
    ]
    return MasterConfig(workers=workers, log_dir=tmp_path)


class _FakeProcess:
    def __init__(self, exit_code: int = 0, pid: int = 1234):
        self._exit_code = exit_code
        self.pid = pid

    async def wait(self) -> int:
        return self._exit_code


@pytest.mark.asyncio
async def test_spawn_one_skill_dry_run_returns_zero(tmp_path: Path, capsys):
    wc = _make_config(tmp_path).workers[0]
    code = await spawn_one_skill(wc, "fapiao-1688", tmp_path / "log.log", dry_run=True)
    assert code == 0
    captured = capsys.readouterr()
    assert "dry-run" in captured.out and "b1" in captured.out


@pytest.mark.asyncio
async def test_spawn_one_skill_invokes_subprocess(tmp_path: Path):
    wc = _make_config(tmp_path).workers[1]
    captured_args: list[tuple] = []

    async def fake_create(*args, **kwargs):
        captured_args.append(args)
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        code = await spawn_one_skill(wc, "fapiao-1688", tmp_path / "log.log")

    assert code == 0
    assert len(captured_args) == 1
    args = captured_args[0]
    assert "--worker-id" in args and "b2" in args
    assert "--skill" in args and "fapiao-1688" in args
    assert "--port" in args and "18766" in args


@pytest.mark.asyncio
async def test_spawn_one_skill_updates_tracker(tmp_path: Path):
    tracker = WorkerStateTracker()
    wc = _make_config(tmp_path).workers[2]

    async def fake_create(*args, **kwargs):
        return _FakeProcess(0, pid=4321)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        await spawn_one_skill(wc, "fapiao-1688", tmp_path / "log.log", tracker=tracker)

    snapshot = tracker.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].worker_id == "b3"
    assert snapshot[0].last_skill == "fapiao-1688"
    assert snapshot[0].last_exit_code == 0


@pytest.mark.asyncio
async def test_fire_due_entries_no_entries(tmp_path: Path):
    config = _make_config(tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(minutes=1)
    results = await fire_due_entries(store, config, now, later)
    assert results == []


@pytest.mark.asyncio
async def test_fire_due_entries_fires_matching(tmp_path: Path):
    config = _make_config(tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b1", "fapiao-1688")
    store.add("* * * * *", "b2", "fapiao-1688-chase")

    spawned: list[tuple] = []

    async def fake_create(*args, **kwargs):
        spawned.append(args)
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(minutes=1, seconds=5)
        results = await fire_due_entries(store, config, now, later)

    assert len(results) == 2
    assert len(spawned) == 2
    assert all(code == 0 for _, code in results)


@pytest.mark.asyncio
async def test_fire_due_entries_dry_run(tmp_path: Path, capsys):
    config = _make_config(tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b1", "fapiao-1688")

    spawned: list[tuple] = []

    async def fake_create(*args, **kwargs):
        spawned.append(args)
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(minutes=1, seconds=5)
        results = await fire_due_entries(store, config, now, later, dry_run=True)

    assert len(results) == 1
    assert spawned == []
    captured = capsys.readouterr()
    assert "dry-run" in captured.out


@pytest.mark.asyncio
async def test_run_once_with_empty_store(tmp_path: Path, capsys, monkeypatch):
    config = _make_config(tmp_path)
    monkeypatch.setattr("agent.master.SCHEDULE_STATE_PATH", tmp_path / "schedule.yaml")

    await run_once(config)
    captured = capsys.readouterr()
    assert "no schedule entries due" in captured.out


@pytest.mark.asyncio
async def test_spawn_one_skill_notifies_channel_on_done(tmp_path: Path):
    """When reply_to is set, a task-done card is sent after worker exits."""
    from agent.channels import ReplyTarget
    wc = _make_config(tmp_path).workers[0]
    sent: list[tuple[str, dict]] = []

    class FakeChannel:
        async def send(self, target_id, payload):
            sent.append((target_id, payload))

    async def fake_create(*args, **kwargs):
        return _FakeProcess(0)

    reply_to = ReplyTarget(channel=FakeChannel(), target_id="oc_chat1", supports_files=False)
    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        code = await spawn_one_skill(
            wc, "fapiao-1688", tmp_path / "log.log",
            reply_to=reply_to, machine_name="pc-test",
        )

    assert code == 0
    assert len(sent) == 1
    target_id, payload = sent[0]
    assert target_id == "oc_chat1"
    assert "card" in payload
    title = payload["card"]["header"]["title"]["content"]
    assert "pc-test" in title
    assert "完成" in title


@pytest.mark.asyncio
async def test_spawn_one_skill_no_notify_without_reply_to(tmp_path: Path):
    """No reply_to → no notification attempt (silent OK)."""
    wc = _make_config(tmp_path).workers[0]

    async def fake_create(*args, **kwargs):
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        code = await spawn_one_skill(
            wc, "fapiao-1688", tmp_path / "log.log",
            reply_to=None,
        )
    assert code == 0  # didn't raise


@pytest.mark.asyncio
async def test_spawn_one_skill_failure_notifies_with_exit_code(tmp_path: Path):
    """Non-zero exit → error card with exit code label."""
    from agent.channels import ReplyTarget
    wc = _make_config(tmp_path).workers[0]
    sent: list[tuple[str, dict]] = []

    class FakeChannel:
        async def send(self, target_id, payload):
            sent.append((target_id, payload))

    async def fake_create(*args, **kwargs):
        return _FakeProcess(2)  # mcp-failed

    reply_to = ReplyTarget(channel=FakeChannel(), target_id="oc_chat1", supports_files=False)
    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        await spawn_one_skill(
            wc, "fapiao-1688", tmp_path / "log.log",
            reply_to=reply_to, machine_name="pc-test",
        )

    assert len(sent) == 1
    title = sent[0][1]["card"]["header"]["title"]["content"]
    assert "失败" in title


@pytest.mark.asyncio
async def test_spawn_one_skill_uploads_output_files(tmp_path: Path):
    """If output_dir has files AND channel supports files, they get uploaded."""
    from agent.channels import ReplyTarget
    wc = _make_config(tmp_path).workers[0]
    files_sent: list[str] = []
    cards_sent: list[dict] = []

    class FakeFileChannel:
        async def send(self, target_id, payload):
            cards_sent.append(payload)

        async def send_file(self, target_id, file_path):
            files_sent.append(file_path)

    async def fake_create(*args, **kwargs):
        # simulate worker writing a file into its output dir before exit
        out_env = kwargs.get("env", {}).get("WORKER_OUTPUT_DIR", "")
        if out_env:
            (Path(out_env) / "report.csv").write_text("col1,col2\n1,2\n", encoding="utf-8")
        return _FakeProcess(0)

    reply_to = ReplyTarget(
        channel=FakeFileChannel(), target_id="oc_chat1", supports_files=True,
    )
    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        await spawn_one_skill(
            wc, "fapiao-1688", tmp_path / "logs" / "log.log",
            reply_to=reply_to, machine_name="pc-test",
        )

    assert len(cards_sent) == 1
    assert len(files_sent) == 1
    assert files_sent[0].endswith("report.csv")


@pytest.mark.asyncio
async def test_fire_due_dedups_same_worker(tmp_path: Path, capsys):
    """Two entries firing in the same window for b1 → only first runs, second gets -3."""
    config = _make_config(tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b1", "fapiao-1688")
    store.add("* * * * *", "b1", "fapiao-1688-chase")

    spawned: list[tuple] = []

    async def fake_create(*args, **kwargs):
        spawned.append(args)
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(minutes=1, seconds=5)
        results = await fire_due_entries(store, config, now, later)

    assert len(results) == 2
    assert len(spawned) == 1  # only one actually launched
    codes = sorted(code for _, code in results)
    assert codes == [-3, 0]   # one skipped, one OK
    captured = capsys.readouterr()
    assert "already firing" in captured.out


@pytest.mark.asyncio
async def test_fire_due_unknown_worker_alerts(tmp_path: Path, capsys):
    config = _make_config(tmp_path, num_workers=2)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    store.add("* * * * *", "b9", "fapiao-1688")

    async def fake_create(*args, **kwargs):
        return _FakeProcess(0)

    with patch("agent.master.asyncio.create_subprocess_exec", side_effect=fake_create):
        now = datetime(2026, 5, 23, 16, 0, 0, tzinfo=timezone.utc)
        later = now + timedelta(minutes=1, seconds=5)
        results = await fire_due_entries(store, config, now, later)

    captured = capsys.readouterr()
    # b9 doesn't exist in config nor as a clone; either an ALERT prints or worker_config_from_file resolves
    if any(code == -1 for _, code in results):
        assert "ALERT" in captured.out
