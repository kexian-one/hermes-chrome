from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.browser_tab_smoke import (
    BrowserTabSmokeResult,
    run_tab_smokes,
    run_worker_tab_smoke,
)
from agent.config import LLMSettings, WorkerConfig
from agent.mcp_client import Tool, ToolResult


_DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test", api_key="test")


def _worker(tmp_path: Path, worker_id: str = "b1") -> WorkerConfig:
    worker_num = int(worker_id.removeprefix("b"))
    script = tmp_path / f"oicc-{worker_id}" / "host" / "mcp-server.js"
    script.parent.mkdir(parents=True)
    script.write_text("// test\n", encoding="utf-8")
    return WorkerConfig(
        worker_id=worker_id,
        mcp_port=18764 + worker_num,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        mcp_server_js_path=script,
    )


@pytest.mark.asyncio
async def test_worker_tab_smoke_creates_navigates_closes_and_verifies(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []
    created = False
    closed = False

    class FakeMCP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def list_tools(self):
            return [
                Tool("tabs_create_mcp", "", {}),
                Tool("tabs_close_mcp", "", {}),
                Tool("navigate", "", {}),
            ]

        async def call_tool(self, name, args):
            nonlocal closed, created
            calls.append((name, args))
            if name == "tabs_context_mcp":
                tabs = [{"tabId": 42}]
                if created and not closed:
                    tabs.append({"tabId": 43})
                return ToolResult(
                    content=[{"type": "text", "text": json.dumps({"availableTabs": tabs})}],
                    is_error=False,
                )
            if name == "tabs_create_mcp":
                created = True
                return ToolResult(
                    content=[{"type": "text", "text": "Created new tab. Tab ID: 43"}],
                    is_error=False,
                )
            if name == "navigate":
                return ToolResult(content=[{"type": "text", "text": "Navigated"}], is_error=False)
            if name == "tabs_close_mcp":
                assert args == {"tabId": 43}
                closed = True
                return ToolResult(content=[{"type": "text", "text": "Closed tab 43."}], is_error=False)
            raise AssertionError(name)

    with (
        patch("agent.browser_tab_smoke.OpenClaudeInChromeClient", return_value=FakeMCP()),
        patch("agent.browser_tab_smoke.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await run_worker_tab_smoke(_worker(tmp_path))

    assert result.ok is True
    assert result.tab_id == 43
    assert result.after_tabs == (42,)
    assert ("tabs_close_mcp", {"tabId": 43}) in calls
    navigate_args = next(args for name, args in calls if name == "navigate")
    assert navigate_args["tabId"] == 43
    assert "all_in_ai_tab_smoke=b1_" in navigate_args["url"]


@pytest.mark.asyncio
async def test_worker_tab_smoke_requires_existing_primary_listener(tmp_path: Path) -> None:
    with patch(
        "agent.browser_tab_smoke._wait_for_tcp_port",
        new_callable=AsyncMock,
        return_value=False,
    ):
        result = await run_worker_tab_smoke(
            _worker(tmp_path),
            require_listener=True,
            timeout_seconds=0.1,
        )

    assert result.ok is False
    assert "no primary listener" in result.error


@pytest.mark.asyncio
async def test_run_tab_smokes_checks_b1_to_b6_in_order(tmp_path: Path) -> None:
    workers = [_worker(tmp_path, f"b{i}") for i in range(1, 7)]
    seen: list[tuple[str, bool, float]] = []

    async def fake_run(wc: WorkerConfig, *, require_listener: bool, timeout_seconds: float):
        seen.append((wc.worker_id, require_listener, timeout_seconds))
        return BrowserTabSmokeResult(worker_id=wc.worker_id, ok=True)

    with patch("agent.browser_tab_smoke.run_worker_tab_smoke", side_effect=fake_run):
        results = await run_tab_smokes(workers, require_listener=True, timeout_seconds=3)

    assert [result.worker_id for result in results] == [f"b{i}" for i in range(1, 7)]
    assert seen == [(f"b{i}", True, 3) for i in range(1, 7)]


@pytest.mark.asyncio
async def test_browser_tab_smoke_cli_defaults_to_b1_through_b6(tmp_path: Path, monkeypatch) -> None:
    import scripts.browser_tab_smoke as cli

    workers = [_worker(tmp_path, f"b{i}") for i in range(1, 7)]
    captured: dict[str, object] = {}

    async def fake_run_tab_smokes(
        selected_workers: list[WorkerConfig],
        *,
        require_listener: bool,
        timeout_seconds: float,
    ):
        captured["worker_ids"] = [wc.worker_id for wc in selected_workers]
        captured["require_listener"] = require_listener
        captured["timeout_seconds"] = timeout_seconds
        return [BrowserTabSmokeResult(worker_id=wc.worker_id, ok=True) for wc in selected_workers]

    monkeypatch.setattr(cli, "default_master_config", lambda: SimpleNamespace(workers=workers))
    monkeypatch.setattr(cli, "run_tab_smokes", fake_run_tab_smokes)
    monkeypatch.setattr(sys, "argv", ["browser_tab_smoke.py", "--timeout", "7"])

    code = await cli._amain()

    assert code == 0
    assert captured == {
        "worker_ids": [f"b{i}" for i in range(1, 7)],
        "require_listener": False,
        "timeout_seconds": 7,
    }
