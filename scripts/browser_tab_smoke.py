from __future__ import annotations

import argparse
import asyncio
import sys

from agent.browser_tab_smoke import run_tab_smokes
from agent.config import default_master_config


def _parse_workers(raw: str) -> set[str] | None:
    value = raw.strip()
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Verify each browser MCP can create and close a temporary tab.")
    parser.add_argument("--workers", default="", help="Comma-separated worker ids, e.g. b1,b2. Defaults to all workers.")
    parser.add_argument("--timeout", type=float, default=45, help="Per-worker timeout in seconds.")
    parser.add_argument(
        "--require-listener",
        action="store_true",
        help="Require an existing primary MCP listener before opening a client session.",
    )
    args = parser.parse_args()

    config = default_master_config()
    selected = _parse_workers(args.workers)
    workers = [wc for wc in config.workers if selected is None or wc.worker_id in selected]
    if not workers:
        print("[browser-tab-smoke] no workers selected", file=sys.stderr)
        return 2

    print(f"[browser-tab-smoke] checking {', '.join(w.worker_id for w in workers)}")
    results = await run_tab_smokes(
        workers,
        require_listener=args.require_listener,
        timeout_seconds=args.timeout,
    )
    for result in results:
        print(f"[browser-tab-smoke] {result.line()}")
    return 0 if all(result.ok for result in results) else 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
