from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.intents import MasterDispatcher, handle
from agent.nlu import Intent, IntentDispatch
from agent.schedule_store import ScheduleStore
from agent.worker_state import WorkerState, WorkerStateTracker


def _make_dispatcher(
    tmp_path: Path,
    tracker: WorkerStateTracker | None = None,
    schedule_path: Path | None = None,
) -> MasterDispatcher:
    dispatcher = MagicMock(spec=MasterDispatcher)
    dispatcher.worker_state = tracker or WorkerStateTracker()
    dispatcher.log_dir = tmp_path
    dispatcher.schedule_store = ScheduleStore(schedule_path or (tmp_path / "schedule.yaml"))
    dispatcher.knowledge_root = tmp_path / "knowledge"
    dispatcher.skills_dir = tmp_path / "skills"
    dispatcher.restart_worker = AsyncMock()
    dispatcher.spawn_now = AsyncMock()
    dispatcher.spawn_freeform = AsyncMock()
    dispatcher.set_paused = AsyncMock()
    dispatcher.restart_self = AsyncMock()
    dispatcher.configured_browser_ids = ()
    dispatcher.probe_extension_connectivity = AsyncMock(return_value=True)
    return dispatcher


MACHINE = "pc-test"


def _all_text(card: dict) -> str:
    """Flatten card title + element contents for substring assertions."""
    parts = [card["header"]["title"]["content"]]
    for e in card.get("elements", []):
        parts.append(str(e.get("content", "")))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_query_status_no_workers(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_STATUS, {}), d, MACHINE)
    assert "暂无" in _all_text(reply)
    assert MACHINE in _all_text(reply)


@pytest.mark.asyncio
async def test_query_status_with_workers(tmp_path: Path):
    tracker = WorkerStateTracker()
    tracker.update_spawn("b1", "fapiao-1688", 1234)
    tracker.update_spawn("b2", "fapiao-1688-chase", 5678)
    d = _make_dispatcher(tmp_path, tracker)
    reply = await handle(IntentDispatch(Intent.QUERY_STATUS, {}), d, MACHINE)
    assert "b1" in _all_text(reply)
    assert "b2" in _all_text(reply)
    assert "运行中" in _all_text(reply)
    assert MACHINE in _all_text(reply)


@pytest.mark.asyncio
async def test_restart_worker_with_id(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.RESTART_WORKER, {"worker_id": "b3"}), d, MACHINE)
    d.restart_worker.assert_called_once_with("b3", None)
    assert "b3" in _all_text(reply)
    assert MACHINE in _all_text(reply)


@pytest.mark.asyncio
async def test_restart_worker_missing_id(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.RESTART_WORKER, {}), d, MACHINE)
    d.restart_worker.assert_not_called()
    assert "指定" in _all_text(reply)


@pytest.mark.asyncio
async def test_query_logs_no_log_file(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_LOGS, {"worker_id": "b1"}), d, MACHINE)
    assert "未找到" in _all_text(reply) or "b1" in _all_text(reply)


@pytest.mark.asyncio
async def test_query_logs_with_file(tmp_path: Path):
    log_file = tmp_path / "worker-b1-20260101T000000Z.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_LOGS, {"worker_id": "b1"}), d, MACHINE)
    assert "line1" in _all_text(reply)
    assert "b1" in _all_text(reply)


@pytest.mark.asyncio
async def test_query_logs_missing_id(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_LOGS, {}), d, MACHINE)
    assert "指定" in _all_text(reply)


@pytest.mark.asyncio
async def test_query_stats_no_logs(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_STATS, {}), d, MACHINE)
    assert "暂无" in _all_text(reply) or "日志" in _all_text(reply)
    assert MACHINE in _all_text(reply)


@pytest.mark.asyncio
async def test_query_stats_with_logs(tmp_path: Path):
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    log = tmp_path / f"worker-b1-{today}T120000Z.log"
    log.write_text(
        "exit=0 (OK)\nexit=0 (OK)\nexit=1 (skill-failed)\n",
        encoding="utf-8",
    )
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.QUERY_STATS, {}), d, MACHINE)
    assert "统计" in _all_text(reply)
    assert "2" in _all_text(reply)  # 2 successes


@pytest.mark.asyncio
async def test_pause_all(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.PAUSE_ALL, {}), d, MACHINE)
    d.set_paused.assert_called_once_with(True)
    assert "暂停" in _all_text(reply)


@pytest.mark.asyncio
async def test_resume_all(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.RESUME_ALL, {}), d, MACHINE)
    d.set_paused.assert_called_once_with(False)
    assert "恢复" in _all_text(reply)


@pytest.mark.asyncio
async def test_help(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.HELP, {}), d, MACHINE)
    assert "查状态" in _all_text(reply)
    assert "重启" in _all_text(reply)
    assert MACHINE in _all_text(reply)


@pytest.mark.asyncio
async def test_unknown(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.UNKNOWN, {}), d, MACHINE)
    assert "没听懂" in _all_text(reply)
    assert "/help" in _all_text(reply)


@pytest.mark.asyncio
async def test_run_now_dispatches(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.RUN_NOW, {"worker_id": "b2", "skill": "fapiao-1688"}),
        d, MACHINE,
    )
    d.spawn_now.assert_called_once_with("b2", "fapiao-1688", None)
    assert "b2" in _all_text(reply) and "fapiao-1688" in _all_text(reply)


@pytest.mark.asyncio
async def test_run_now_rejects_bad_worker(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.RUN_NOW, {"worker_id": "bogus", "skill": "fapiao-1688"}),
        d, MACHINE,
    )
    d.spawn_now.assert_not_called()
    assert "b1-b6" in _all_text(reply)


@pytest.mark.asyncio
async def test_run_now_rejects_bad_skill(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.RUN_NOW, {"worker_id": "b2", "skill": "BAD SKILL"}),
        d, MACHINE,
    )
    d.spawn_now.assert_not_called()
    assert "skill" in _all_text(reply)


@pytest.mark.asyncio
async def test_schedule_add_persists(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.SCHEDULE_ADD, {
            "cron": "0 16 * * *",
            "worker_id": "b2",
            "skill": "fapiao-1688",
        }),
        d, MACHINE,
    )
    assert "#1" in _all_text(reply)
    assert "b2" in _all_text(reply)
    entries = d.schedule_store.list_all()
    assert len(entries) == 1
    assert entries[0].worker == "b2"


@pytest.mark.asyncio
async def test_schedule_add_invalid_cron(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.SCHEDULE_ADD, {
            "cron": "garbage",
            "worker_id": "b2",
            "skill": "fapiao-1688",
        }),
        d, MACHINE,
    )
    assert "无效" in _all_text(reply)
    assert len(d.schedule_store.list_all()) == 0


@pytest.mark.asyncio
async def test_schedule_list_empty(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.SCHEDULE_LIST, {}), d, MACHINE)
    assert "没有定时任务" in _all_text(reply)


@pytest.mark.asyncio
async def test_schedule_list_with_entries(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    d.schedule_store.add("0 9 * * *", "b1", "fapiao-1688")
    d.schedule_store.add("0 16 * * *", "b2", "fapiao-1688-chase")
    reply = await handle(IntentDispatch(Intent.SCHEDULE_LIST, {}), d, MACHINE)
    assert "#1" in _all_text(reply) and "#2" in _all_text(reply)
    assert "b1" in _all_text(reply) and "b2" in _all_text(reply)


@pytest.mark.asyncio
async def test_schedule_remove_existing(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    entry = d.schedule_store.add("0 9 * * *", "b1", "fapiao-1688")
    reply = await handle(
        IntentDispatch(Intent.SCHEDULE_REMOVE, {"entry_id": entry.id}), d, MACHINE
    )
    assert "已删除" in _all_text(reply)
    assert d.schedule_store.list_all() == []


@pytest.mark.asyncio
async def test_schedule_remove_missing(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.SCHEDULE_REMOVE, {"entry_id": 999}), d, MACHINE
    )
    assert "没找到" in _all_text(reply)


@pytest.mark.asyncio
async def test_schedule_remove_invalid_id(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.SCHEDULE_REMOVE, {"entry_id": "abc"}), d, MACHINE
    )
    assert "数字" in _all_text(reply)


@pytest.mark.asyncio
async def test_freeform_dispatches(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.FREEFORM, {"worker_id": "b2", "task": "去京东看订单"}),
        d, MACHINE,
    )
    d.spawn_freeform.assert_called_once_with("b2", "去京东看订单", None)
    assert "freeform" in _all_text(reply)


@pytest.mark.asyncio
async def test_freeform_rejects_empty_task(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(
        IntentDispatch(Intent.FREEFORM, {"worker_id": "b2", "task": ""}),
        d, MACHINE,
    )
    d.spawn_freeform.assert_not_called()
    assert "任务" in _all_text(reply)


@pytest.mark.asyncio
async def test_skill_list_when_skills_present(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    skill_dir = skills_root / "fapiao-1688"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fapiao-1688\ndescription: 抓 1688 发票\n---\nbody",
        encoding="utf-8",
    )
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    d = _make_dispatcher(logs_dir)
    d.skills_dir = skills_root
    reply = await handle(IntentDispatch(Intent.SKILL_LIST, {}), d, MACHINE)
    assert "fapiao-1688" in _all_text(reply)


@pytest.mark.asyncio
async def test_skill_list_when_empty(tmp_path: Path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    d = _make_dispatcher(tmp_path)
    d.skills_dir = skills_root
    reply = await handle(IntentDispatch(Intent.SKILL_LIST, {}), d, MACHINE)
    assert "还没安装" in _all_text(reply)


@pytest.mark.asyncio
async def test_restart_self_delegates_to_master(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    from agent.cards import success_card
    d.restart_self = AsyncMock(return_value=success_card("ok", "body"))
    reply = await handle(IntentDispatch(Intent.RESTART_SELF, {}), d, MACHINE)
    d.restart_self.assert_awaited_once_with(None)
    assert "ok" in _all_text(reply)


@pytest.mark.asyncio
async def test_help_mentions_restart_self(tmp_path: Path):
    d = _make_dispatcher(tmp_path)
    reply = await handle(IntentDispatch(Intent.HELP, {}), d, MACHINE)
    assert "重启你自己" in _all_text(reply)
