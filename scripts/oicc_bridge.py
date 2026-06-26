from __future__ import annotations

import argparse
import sys

from agent.config import default_master_config
from agent.oicc_bridge import run_supervisor, start_daemon, status, stop_daemon, supervisor_running


def _print_status() -> int:
    config = default_master_config()
    print(f"supervisor: {'running' if supervisor_running(config.project_root) else 'stopped'}")
    rows = status(config)
    for row in rows:
        state = "OK" if row.running else "DOWN"
        detail = f" pid={row.pid}" if row.pid else ""
        listener = f" listener={row.listening_pid}" if row.listening_pid else ""
        reason = f" ({row.reason})" if row.reason else ""
        print(f"{row.worker_id}: {state} port={row.port}{detail}{listener}{reason}")
    return 0 if all(row.running for row in rows) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the independent OICC browser MCP bridge.")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "run"])
    args = parser.parse_args()

    config = default_master_config()

    if args.command == "run":
        raise SystemExit(run_supervisor(config))
    if args.command == "start":
        pid = start_daemon(config)
        print(f"oicc-bridge supervisor pid={pid}")
        raise SystemExit(_print_status())
    if args.command == "stop":
        stop_daemon(config)
        print("oicc-bridge stopped")
        raise SystemExit(0)
    if args.command == "restart":
        stop_daemon(config)
        pid = start_daemon(config)
        print(f"oicc-bridge supervisor pid={pid}")
        raise SystemExit(_print_status())
    if args.command == "status":
        raise SystemExit(_print_status())

    raise SystemExit(2)


if __name__ == "__main__":
    main()
