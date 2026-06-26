from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent.config import LLMSettings, MasterConfig, WorkerConfig
from agent.oicc_bridge import start_daemon, status, stop_daemon, supervisor_pidfile, worker_pidfile


_DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test", api_key="test")


def _config(tmp_path: Path) -> MasterConfig:
    workers = [
        WorkerConfig(
            worker_id=f"b{i}",
            mcp_port=18764 + i,
            llm_multimodal=_DUMMY_LLM,
            llm_reasoning=_DUMMY_LLM,
            mcp_server_js_path=tmp_path / f"oicc-b{i}" / "host" / "mcp-server.js",
        )
        for i in range(1, 3)
    ]
    return MasterConfig(workers=workers, project_root=tmp_path, log_dir=tmp_path / "logs")


def test_status_matches_pidfiles_to_listening_ports(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker_pidfile(tmp_path, "b1").parent.mkdir(parents=True)
    worker_pidfile(tmp_path, "b1").write_text("111", encoding="utf-8")
    worker_pidfile(tmp_path, "b2").write_text("222", encoding="utf-8")

    with (
        patch("agent.oicc_bridge._pid_running", side_effect=lambda pid: pid in {111, 222}),
        patch("agent.oicc_bridge._find_pids_on_ports", return_value={18765: 111}),
        patch("agent.oicc_bridge._get_process_cmdline", return_value="node deploy/oicc-b1/host/mcp-server.js"),
    ):
        rows = status(config)

    assert rows[0].running is True
    assert rows[0].pid == 111
    assert rows[1].running is False
    assert rows[1].reason == "not listening"


def test_start_daemon_is_idempotent_when_supervisor_is_running(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor_pidfile(tmp_path).parent.mkdir(parents=True)
    supervisor_pidfile(tmp_path).write_text("999", encoding="utf-8")

    with (
        patch("agent.oicc_bridge._pid_running", return_value=True),
        patch("agent.oicc_bridge.subprocess.Popen") as popen,
    ):
        pid = start_daemon(config)

    assert pid == 999
    popen.assert_not_called()


def test_stop_daemon_terminates_supervisor_and_worker_pidfiles(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor_pidfile(tmp_path).parent.mkdir(parents=True)
    supervisor_pidfile(tmp_path).write_text("999", encoding="utf-8")
    worker_pidfile(tmp_path, "b1").write_text("111", encoding="utf-8")

    killed: list[int] = []
    with patch("agent.oicc_bridge._terminate_pid", side_effect=lambda pid: killed.append(pid)):
        stop_daemon(config)

    assert killed == [999, 111]
    assert not supervisor_pidfile(tmp_path).exists()
    assert not worker_pidfile(tmp_path, "b1").exists()
