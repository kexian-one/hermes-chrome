from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from agent.builtin_tools import (
    _close_temporary_browser_tab,
    _extract_oicc_tab_ids,
    _mcp_call_text,
    _open_temporary_browser_tab,
)
from agent.config import WorkerConfig
from agent.mcp_client import OpenClaudeInChromeClient


@dataclass(frozen=True)
class BrowserTabSmokeResult:
    worker_id: str
    ok: bool
    tab_id: int | None = None
    method: str = ""
    error: str = ""
    before_tabs: tuple[int, ...] = ()
    after_tabs: tuple[int, ...] = ()

    def line(self) -> str:
        if self.ok:
            return (
                f"{self.worker_id}: OK tab={self.tab_id} closed via {self.method} "
                f"remaining={list(self.after_tabs)}"
            )
        detail = f" error={self.error}" if self.error else ""
        return f"{self.worker_id}: FAIL tab={self.tab_id}{detail} remaining={list(self.after_tabs)}"


async def _tcp_port_is_open(port: int) -> bool:
    def check() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            return False

    return await asyncio.to_thread(check)


async def _wait_for_tcp_port(port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if await _tcp_port_is_open(port):
            return True
        await asyncio.sleep(0.5)
    return False


async def run_worker_tab_smoke(
    wc: WorkerConfig,
    *,
    require_listener: bool = False,
    timeout_seconds: float = 45,
    settle_seconds: float = 1.0,
) -> BrowserTabSmokeResult:
    async def _run() -> BrowserTabSmokeResult:
        if not wc.mcp_server_js_path.is_file():
            return BrowserTabSmokeResult(
                worker_id=wc.worker_id,
                ok=False,
                error=f"mcp-server.js missing: {wc.mcp_server_js_path}",
            )
        if require_listener and not await _wait_for_tcp_port(wc.mcp_port, timeout_seconds):
            return BrowserTabSmokeResult(
                worker_id=wc.worker_id,
                ok=False,
                error=f"no primary listener on port {wc.mcp_port}",
            )

        async with OpenClaudeInChromeClient(
            port=wc.mcp_port,
            mcp_server_js_path=Path(wc.mcp_server_js_path),
            require_bridge=require_listener,
        ) as mcp:
            tool_names = {tool.name for tool in await mcp.list_tools()}
            missing = {"tabs_create_mcp", "tabs_close_mcp", "navigate"} - tool_names
            if missing:
                return BrowserTabSmokeResult(
                    worker_id=wc.worker_id,
                    ok=False,
                    error=f"missing tools: {', '.join(sorted(missing))}",
                )

            before_text = await _mcp_call_text(mcp, "tabs_context_mcp", {})
            before_tabs = tuple(_extract_oicc_tab_ids(before_text))
            tab_id, create_text = await _open_temporary_browser_tab(mcp)
            if tab_id is None:
                return BrowserTabSmokeResult(
                    worker_id=wc.worker_id,
                    ok=False,
                    error=(create_text or "could not create temporary tab")[:500],
                    before_tabs=before_tabs,
                )

            url = f"https://example.com/?all_in_ai_tab_smoke={wc.worker_id}_{int(time.time())}"
            await _mcp_call_text(mcp, "navigate", {"tabId": tab_id, "url": url})
            await asyncio.sleep(settle_seconds)
            close_status = await _close_temporary_browser_tab(mcp, tab_id)
            after_text = await _mcp_call_text(mcp, "tabs_context_mcp", {})
            after_tabs = tuple(_extract_oicc_tab_ids(after_text))
            ok = bool(close_status.get("ok")) and tab_id not in after_tabs
            return BrowserTabSmokeResult(
                worker_id=wc.worker_id,
                ok=ok,
                tab_id=tab_id,
                method=str(close_status.get("method") or ""),
                error="" if ok else str(close_status.get("error") or close_status.get("message") or "close failed")[:500],
                before_tabs=before_tabs,
                after_tabs=after_tabs,
            )

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_seconds + 10)
    except Exception as exc:
        return BrowserTabSmokeResult(
            worker_id=wc.worker_id,
            ok=False,
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
        )


async def run_tab_smokes(
    workers: list[WorkerConfig],
    *,
    require_listener: bool = False,
    timeout_seconds: float = 45,
) -> list[BrowserTabSmokeResult]:
    results: list[BrowserTabSmokeResult] = []
    for wc in workers:
        result = await run_worker_tab_smoke(
            wc,
            require_listener=require_listener,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)
    return results
