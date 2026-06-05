from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.mcp_client import OpenClaudeInChromeClient, tools_to_openai_functions

DEFAULT_INSTANCE = "b2"
INSTANCE = os.environ.get("MCP_TEST_INSTANCE", DEFAULT_INSTANCE)
PORT = int(os.environ.get("MCP_TEST_PORT", str(18764 + int(INSTANCE.lstrip("b")))))

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "deploy" / f"oicc-{INSTANCE}" / "host" / "mcp-server.js"

pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.is_file(),
    reason=(
        f"mcp-server.js not found at {SCRIPT_PATH}. "
        f"Run deploy/clone-oicc.ps1 (Windows) or deploy/clone-oicc.sh (macOS) first, "
        f"or set MCP_TEST_INSTANCE."
    ),
)


async def _connect_or_skip():
    try:
        client = OpenClaudeInChromeClient(port=PORT, mcp_server_js_path=SCRIPT_PATH)
        await client.__aenter__()
        return client
    except Exception as exc:
        pytest.skip(
            f"could not spawn / handshake mcp-server.js for {INSTANCE} on port {PORT}: "
            f"{type(exc).__name__}: {exc}. Common causes: node not installed, "
            f"port already bound by another instance, npm install not run in host/."
        )


async def test_can_spawn_and_list_tools() -> None:
    client = await _connect_or_skip()
    try:
        tools = await client.list_tools()
        assert tools, "MCP server returned empty tool list"
        print(f"\n  instance={INSTANCE} port={PORT}")
        print(f"  script={SCRIPT_PATH}")
        print(f"  discovered {len(tools)} tools:")
        for t in tools[:15]:
            print(f"    {t.name}")
        if len(tools) > 15:
            print(f"    ... and {len(tools) - 15} more")
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass


async def test_tools_convert_to_openai_function_schema() -> None:
    client = await _connect_or_skip()
    try:
        tools = await client.list_tools()
        openai_fns = tools_to_openai_functions(tools)
        assert len(openai_fns) == len(tools)
        for fn in openai_fns:
            assert fn["type"] == "function"
            assert isinstance(fn["function"]["name"], str)
            assert isinstance(fn["function"]["parameters"], dict)
    finally:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass
