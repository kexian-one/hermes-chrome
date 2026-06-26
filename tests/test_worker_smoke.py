from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import LLMSettings, WorkerConfig
from agent.llm_client import ChatResponse, ToolCall
from agent.mcp_client import Tool, ToolResult
from agent.worker import _load_skill_body, run

_DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test-model", api_key="test-key")


@pytest.fixture()
def skills_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "fapiao-1688"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: fapiao-1688
            description: 抓取 1688 申请中发票
            ---

            ## 执行步骤

            用 tabs_context_mcp 获取 tab ID。
        """),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def config(skills_dir: Path) -> WorkerConfig:
    return WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=tmp_path_factory_log(),
    )


def tmp_path_factory_log() -> Path:
    import tempfile
    return Path(tempfile.gettempdir())


def make_tool() -> Tool:
    return Tool(
        name="mcp__open-claude-in-chrome__tabs_context_mcp",
        description="Get tab context",
        input_schema={"type": "object", "properties": {"createIfEmpty": {"type": "boolean"}}},
    )


async def _run_with_mocks(
    skills_dir: Path,
    llm_responses: list[ChatResponse],
    mcp_tools: list[Tool],
    mcp_tool_result: ToolResult,
) -> int:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )

    response_iter = iter(llm_responses)

    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=lambda *a, **kw: next(response_iter))

    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=mcp_tools)
    mock_mcp.call_tool = AsyncMock(return_value=mcp_tool_result)

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp),
    ):
        return await run(config, "test system prompt", "fapiao-1688")


@pytest.mark.asyncio
async def test_one_tool_call_then_stop(skills_dir: Path) -> None:
    tool = make_tool()
    tool_call_response = ChatResponse(
        text=None,
        tool_calls=[ToolCall(
            id="call_1",
            name="mcp__open-claude-in-chrome__tabs_context_mcp",
            arguments=json.dumps({"createIfEmpty": True}),
        )],
        finish_reason="tool_calls",
    )
    stop_response = ChatResponse(
        text="完成",
        tool_calls=[],
        finish_reason="stop",
    )
    tool_result = ToolResult(
        content=[{"type": "text", "text": '{"tabId": 42}'}],
        is_error=False,
    )

    exit_code = await _run_with_mocks(
        skills_dir=skills_dir,
        llm_responses=[tool_call_response, stop_response],
        mcp_tools=[tool],
        mcp_tool_result=tool_result,
    )

    assert exit_code == 0


@pytest.mark.asyncio
async def test_immediate_stop(skills_dir: Path) -> None:
    stop_response = ChatResponse(
        text="任务完成",
        tool_calls=[],
        finish_reason="stop",
    )

    exit_code = await _run_with_mocks(
        skills_dir=skills_dir,
        llm_responses=[stop_response],
        mcp_tools=[],
        mcp_tool_result=ToolResult(content=[], is_error=False),
    )

    assert exit_code == 0


def test_missing_skill_returns_none(skills_dir: Path) -> None:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    assert _load_skill_body(config, "nonexistent-skill") is None


def test_existing_skill_loads(skills_dir: Path) -> None:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    loaded = _load_skill_body(config, "fapiao-1688")
    assert loaded is not None
    body, name, requires_browser_mcp = loaded
    assert name == "fapiao-1688"
    assert "执行步骤" in body
    assert requires_browser_mcp is True


@pytest.mark.asyncio
async def test_run_without_browser_mcp_does_not_connect(skills_dir: Path) -> None:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value=ChatResponse(
        text="任务完成",
        tool_calls=[],
        finish_reason="stop",
    ))

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", side_effect=AssertionError("MCP should not connect")),
    ):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=False,
        )

    assert exit_code == 0


@pytest.mark.asyncio
async def test_ecom_requires_csv_before_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "outputs" / "b1-test"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")

    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    responses = iter([
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_1",
                name="write_file",
                arguments=json.dumps({
                    "path": "找货_测试_20260615.csv",
                    "content": "\ufeff排名,商品标题\n1,测试商品\n",
                }),
            )],
            finish_reason="tool_calls",
        ),
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
    ])
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=lambda *a, **kw: next(responses))

    with patch("agent.worker.LLMClient", return_value=mock_llm):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=False,
        )

    assert exit_code == 0
    assert (output_dir / "找货_测试_20260615.csv").is_file()
    assert mock_llm.chat.call_count == 3


@pytest.mark.asyncio
async def test_ecom_stops_before_post_csv_verification_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs" / "b1-test"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")

    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    responses = iter([
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_1",
                name="write_file",
                arguments=json.dumps({
                    "path": "找货_测试_20260615.csv",
                    "content": "\ufeff排名,商品标题\n1,测试商品\n",
                }),
            )],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_2",
                name="read_file",
                arguments=json.dumps({"path": "skills/ecom-best-source/references/final_filter_rules.md"}),
            )],
            finish_reason="tool_calls",
        ),
    ])
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=lambda *a, **kw: next(responses))

    with patch("agent.worker.LLMClient", return_value=mock_llm):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=False,
        )

    assert exit_code == 0
    assert (output_dir / "找货_测试_20260615.csv").is_file()
    assert mock_llm.chat.call_count == 2


@pytest.mark.asyncio
async def test_ecom_fails_when_csv_never_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "outputs" / "b1-test"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")

    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=[
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
    ])

    with patch("agent.worker.LLMClient", return_value=mock_llm):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=False,
        )

    assert exit_code == 1
    assert list(output_dir.glob("*.csv")) == []


@pytest.mark.asyncio
async def test_mcp_connect_failure_returns_2(skills_dir: Path) -> None:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=skills_dir,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    mock_mcp = MagicMock()
    mock_mcp.__aenter__ = AsyncMock(side_effect=OSError("connection refused"))

    with patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp):
        exit_code = await run(config, "test prompt", "fapiao-1688")

    assert exit_code == 2


@pytest.mark.asyncio
async def test_ecom_browser_mcp_uses_configured_worker_port(tmp_path: Path) -> None:
    config = WorkerConfig(
        worker_id="b3",
        mcp_port=18767,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(return_value=ChatResponse(
        text="任务完成",
        tool_calls=[],
        finish_reason="stop",
    ))
    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[])

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp) as mcp_cls,
    ):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=True,
        )

    assert exit_code == 0
    mcp_cls.assert_called_once()
    assert mcp_cls.call_args.kwargs["port"] == 18767
    assert mcp_cls.call_args.kwargs["require_bridge"] is True


@pytest.mark.asyncio
async def test_browser_builtin_tool_is_available_when_browser_mcp_enabled(tmp_path: Path) -> None:
    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    captured_tools: list[dict] = []

    async def fake_chat(messages, tools=None):
        nonlocal captured_tools
        captured_tools = tools or []
        return ChatResponse(text="任务完成", tool_calls=[], finish_reason="stop")

    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=fake_chat)
    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[])

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp),
    ):
        exit_code = await run(
            config,
            "test prompt",
            "non-ecom-browser-skill",
            requires_browser_mcp=True,
        )

    assert exit_code == 0
    tool_names = {tool["function"]["name"] for tool in captured_tools}
    assert "extract_jd_product_browser" in tool_names


@pytest.mark.asyncio
async def test_browser_builtin_tool_uses_current_mcp_and_writes_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs" / "b1-test"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")

    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    responses = iter([
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_1",
                name="extract_jd_product_browser",
                arguments=json.dumps({
                    "url": "https://b2b.jd.com/goods/goods-detail/10212040410264",
                    "output": "jd_product.json",
                    "wait_seconds": 3,
                }),
            )],
            finish_reason="tool_calls",
        ),
        ChatResponse(text="任务完成", tool_calls=[], finish_reason="stop"),
        ChatResponse(text="任务完成", tool_calls=[], finish_reason="stop"),
        ChatResponse(text="任务完成", tool_calls=[], finish_reason="stop"),
    ])
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=lambda *a, **kw: next(responses))

    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[])

    async def fake_call_tool(name, args):
        if name == "tabs_context_mcp":
            return ToolResult(
                content=[{"type": "text", "text": '{"availableTabs":[{"tabId":42}]}'}],
                is_error=False,
            )
        if name == "tabs_create_mcp":
            return ToolResult(
                content=[{"type": "text", "text": 'Created new tab. Tab ID: 43\n\n{"availableTabs":[{"tabId":42},{"tabId":43}]}'}],
                is_error=False,
            )
        if name == "navigate":
            return ToolResult(content=[{"type": "text", "text": "Navigated"}], is_error=False)
        if name == "javascript_tool":
            return ToolResult(
                content=[{
                    "type": "text",
                    "text": json.dumps(json.dumps({
                        "title": "香丹青咸鸭蛋礼盒",
                        "jd_url": "https://b2b.jd.com/goods/goods-detail/10212040410264",
                        "item_id": "10212040410264",
                        "price": 28.8,
                        "jd_price": 28.8,
                        "price_text": "采购价 ￥28.8",
                        "main_image_url": "https://m.360buyimg.com/n1/jfs/test.jpg",
                        "image_urls": ["https://m.360buyimg.com/n1/jfs/test.jpg"],
                    }, ensure_ascii=False), ensure_ascii=False),
                }],
                is_error=False,
            )
        if name == "tabs_close_mcp":
            return ToolResult(content=[{"type": "text", "text": f"Closed tab {args['tabId']}."}], is_error=False)
        return ToolResult(content=[{"type": "text", "text": ""}], is_error=False)

    mock_mcp.call_tool = AsyncMock(side_effect=fake_call_tool)

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp),
        patch("agent.builtin_tools.asyncio.sleep", new_callable=AsyncMock),
    ):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=True,
        )

    assert exit_code == 1
    product_path = output_dir / ".ecom-scratch" / "jd_product.json"
    assert product_path.is_file()
    product = json.loads(product_path.read_text(encoding="utf-8"))
    assert product["item_id"] == "10212040410264"
    assert product["price"] == 28.8
    calls = [(call.args[0], call.args[1]) for call in mock_mcp.call_tool.call_args_list]
    assert ("tabs_create_mcp", {}) in calls
    navigate = next(args for name, args in calls if name == "navigate")
    assert navigate["tabId"] == 43
    assert navigate["url"] == (
        "https://b2b.jd.com/goods/goods-detail/10212040410264"
        "?sourceurl=/trade/goods-detail&bMallTag=1&buId=456"
    )
    assert ("tabs_close_mcp", {"tabId": 43}) in calls


@pytest.mark.asyncio
async def test_ecom_browser_extract_failure_can_fallback_to_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs" / "b1-test"
    output_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")

    config = WorkerConfig(
        worker_id="b1",
        mcp_port=18765,
        skills_dir=tmp_path,
        llm_multimodal=_DUMMY_LLM,
        llm_reasoning=_DUMMY_LLM,
        log_dir=Path("/tmp"),
    )
    responses = iter([
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_1",
                name="extract_jd_product_browser",
                arguments=json.dumps({"url": "https://item.jd.com/10177709350354.html"}),
            )],
            finish_reason="tool_calls",
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_2",
                name="write_file",
                arguments=json.dumps({
                    "path": "找货_兜底_20260626.csv",
                    "content": "\ufeff1688商品标题,价格(元)\n测试,1\n",
                }),
            )],
            finish_reason="tool_calls",
        ),
        ChatResponse(text="完成", tool_calls=[], finish_reason="stop"),
    ])
    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock(side_effect=lambda *a, **kw: next(responses))
    mock_mcp = AsyncMock()
    mock_mcp.__aenter__ = AsyncMock(return_value=mock_mcp)
    mock_mcp.__aexit__ = AsyncMock(return_value=False)
    mock_mcp.list_tools = AsyncMock(return_value=[])

    with (
        patch("agent.worker.LLMClient", return_value=mock_llm),
        patch("agent.worker.OpenClaudeInChromeClient", return_value=mock_mcp),
        patch(
            "agent.worker.execute_browser_builtin",
            new=AsyncMock(return_value=json.dumps({
                "ok": False,
                "error": "browser extension is not connected",
                "fallback_required": "run jd_product.py",
            })),
        ),
    ):
        exit_code = await run(
            config,
            "test prompt",
            "ecom-best-source",
            requires_browser_mcp=True,
        )

    assert exit_code == 0
    assert (output_dir / "找货_兜底_20260626.csv").is_file()
