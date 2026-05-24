"""Startup health check — probe each worker's MCP server before scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent.config import WorkerConfig
from agent.mcp_client import OpenClaudeInChromeClient


_HEALTH_TIMEOUT_SECS = 5


@dataclass
class WorkerHealth:
    worker_id: str
    healthy: bool
    reason: str = ""


async def _probe_one(wc: WorkerConfig) -> WorkerHealth:
    # CancelledError inherits BaseException (not Exception), so catch it
    # explicitly and convert to an "unhealthy" result. Do NOT catch
    # BaseException broadly — that would swallow KeyboardInterrupt / SystemExit
    # which the user expects to actually interrupt the program.
    try:
        async with asyncio.timeout(_HEALTH_TIMEOUT_SECS):
            async with OpenClaudeInChromeClient(
                port=wc.mcp_port,
                mcp_server_js_path=wc.mcp_server_js_path,
            ) as client:
                await client.list_tools()
        return WorkerHealth(worker_id=wc.worker_id, healthy=True)
    except TimeoutError:
        return WorkerHealth(worker_id=wc.worker_id, healthy=False, reason="MCP unreachable (timeout)")
    except asyncio.CancelledError:
        return WorkerHealth(worker_id=wc.worker_id, healthy=False, reason="MCP unreachable (cancelled mid-probe)")
    except Exception as exc:
        return WorkerHealth(worker_id=wc.worker_id, healthy=False, reason=f"MCP unreachable ({type(exc).__name__})")


async def run_health_checks(workers: list[WorkerConfig]) -> list[WorkerHealth]:
    """Probe all workers in parallel, return results. Never raises."""
    results = await asyncio.gather(*[_probe_one(wc) for wc in workers])
    return list(results)


def log_health_results(results: list[WorkerHealth]) -> set[str]:
    """Print health summary line and return set of unhealthy worker_ids."""
    parts = []
    unhealthy: set[str] = set()
    for r in sorted(results, key=lambda x: x.worker_id):
        if r.healthy:
            parts.append(f"{r.worker_id}=✓")
        else:
            parts.append(f"{r.worker_id}=✗({r.reason})")
            unhealthy.add(r.worker_id)
    print(f"Health: {' '.join(parts)}")
    return unhealthy
