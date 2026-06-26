from __future__ import annotations

import os
import shutil
import socket
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolResult:
    content: list[dict]
    is_error: bool


class OpenClaudeInChromeClient:
    """Spawn mcp-server.js as a subprocess and talk MCP over its stdio.

    mcp-server.js also opens a TCP listener on `port` for the browser native-host
    to connect to (that's the bridge). We use stdio for our own MCP calls;
    TCP is only browser ↔ mcp-server.js plumbing.
    """

    def __init__(
        self,
        port: int,
        mcp_server_js_path: Path,
        node_bin: str = "node",
        extra_env: dict[str, str] | None = None,
        require_bridge: bool = False,
    ) -> None:
        self._port = port
        self._script = Path(mcp_server_js_path)
        self._node = resolve_node_bin(node_bin)
        self._extra_env = dict(extra_env or {})
        self._require_bridge = require_bridge
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "OpenClaudeInChromeClient":
        self._stack = AsyncExitStack()

        if self._require_bridge and not _tcp_port_is_listening(self._port):
            raise OSError(
                f"OICC bridge is not listening on 127.0.0.1:{self._port}. "
                "Start the independent oicc-bridge before connecting."
            )

        env = os.environ.copy()
        env["OICC_PORT"] = str(self._port)
        env.update(self._extra_env)

        params = StdioServerParameters(
            command=self._node,
            args=[str(self._script)],
            env=env,
            cwd=str(self._script.parent),
        )

        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> list[Tool]:
        assert self._session is not None
        result = await self._session.list_tools()
        tools = []
        for t in result.tools:
            schema = t.inputSchema if isinstance(t.inputSchema, dict) else t.inputSchema.model_dump()
            tools.append(Tool(
                name=t.name,
                description=t.description or "",
                input_schema=schema,
            ))
        return tools

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        content = []
        for item in result.content:
            if hasattr(item, "model_dump"):
                content.append(item.model_dump())
            else:
                content.append({"type": "text", "text": str(item)})
        return ToolResult(content=content, is_error=result.isError or False)


def tools_to_openai_functions(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _tcp_port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def resolve_node_bin(node_bin: str = "node") -> str:
    """Resolve node even under launchd's minimal PATH."""
    candidate = Path(node_bin).expanduser()
    if candidate.parent != Path("."):
        return str(candidate)
    resolved = shutil.which(node_bin)
    if resolved:
        return resolved
    for path in (
        Path.home() / ".homebrew/bin" / node_bin,
        Path("/opt/homebrew/bin") / node_bin,
        Path("/usr/local/bin") / node_bin,
    ):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return node_bin
