"""Master orchestrator: schedule-driven worker dispatch + 飞书 bot loop."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Force UTF-8 on stdout/stderr — Windows default is GBK and crashes on Unicode
# glyphs (e.g. ✓, →, emojis). Master prints status lines with these.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# Logging — env var ALL_IN_AI_LOG=DEBUG/INFO/WARNING/ERROR (default INFO).
# Without basicConfig, agent.* loggers run at WARNING and INFO calls are
# invisible. Production users can set ALL_IN_AI_LOG=WARNING to quiet down.
_log_level = os.environ.get("ALL_IN_AI_LOG", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
# Lark / httpx are very chatty at INFO/DEBUG — keep them at WARNING unless
# the user explicitly wants the noise.
if _log_level not in ("DEBUG",):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.channels import ReplyTarget
from agent.config import MasterConfig, WorkerConfig, default_master_config, worker_config_from_file
from agent.schedule_store import ScheduleEntry, ScheduleStore
from agent.worker_state import WorkerStateTracker


SCHEDULE_STATE_PATH = Path("state/schedule.yaml")
CRON_POLL_INTERVAL_SECS = 60


class _MasterDispatcherImpl:
    def __init__(
        self,
        config: MasterConfig,
        tracker: WorkerStateTracker,
        store: ScheduleStore,
        paused_flag: list[bool],
        unhealthy: set[str] | None = None,
    ) -> None:
        self._config = config
        self.worker_state = tracker
        self.log_dir = config.log_dir
        self.schedule_store = store
        self.knowledge_root = config.knowledge.root
        self.skills_dir = config.skills.dir
        # Worker ids the user explicitly configured browsers for. query_status
        # uses this to show "configured-but-not-yet-run" rows alongside the
        # ever-spawned ones, instead of silently dropping b3 just because no
        # task has been dispatched to it yet.
        self.configured_browser_ids: tuple[str, ...] = tuple(sorted(config.browsers.keys()))
        self._paused = paused_flag
        self._unhealthy: set[str] = unhealthy if unhealthy is not None else set()
        # Channels (one per active bot) register themselves here. Used for:
        #   1. Default reply target for cron-fired (no-origin) tasks
        #   2. Multi-bot support (Feishu + DingTalk + WeCom + ...)
        # Stores per-channel: (channel, alert_chat_id, supports_files, machine_name, is_alert_target).
        # Only one channel per machine may be the alert target (enforced at config parse).
        self._channels: list[tuple[Any, str, bool, str, bool]] = []
        # Predicates that race-protect spawn calls: a worker_id is "pending"
        # from the moment a spawn is requested until the subprocess actually
        # exits (or its task is rejected). restart_self / spawn_* check this
        # synchronously alongside `_is_alive` to close the window where
        # `asyncio.create_task` returns but the tracker hasn't been updated yet.
        self._pending: set[str] = set()
        # Same idea for browser restart — without this, an impatient user
        # spamming `[b2] 重启浏览器` runs the 30s kill+relaunch cycle in
        # parallel for the SAME worker, killing each other's freshly-spawned
        # Edge processes and confusing the bridge.
        self._restarting_browser: set[str] = set()
        self._inflight: set[asyncio.Task] = set()

    def register_channel(
        self, channel: Any, alert_chat_id: str, supports_files: bool = False,
        machine_name: str = "", is_alert_target: bool = False,
    ) -> None:
        """Each bot calls this once at startup. Cron-fired tasks notify the
        channel flagged `is_alert_target=true` (or, if none flagged, the first
        registered channel)."""
        self._channels.append((channel, alert_chat_id, supports_files, machine_name, is_alert_target))

    def find_reply_target_by_app_id(
        self, app_id: str, chat_id: str,
    ) -> ReplyTarget | None:
        """Locate a registered channel by its app_id (Feishu-style: each bot
        instance has a unique app_id) and build a ReplyTarget for the given
        chat. Used by cron firing to route results back to the originating
        (bot, group). Returns None if no channel matches — caller falls back
        to default."""
        if not app_id or not chat_id:
            return None
        for channel, _alert_id, supports_files, machine_name, _is_alert in self._channels:
            if getattr(channel, "app_id", "") == app_id:
                return ReplyTarget(
                    channel=channel, target_id=chat_id,
                    supports_files=supports_files, machine_name=machine_name,
                )
        return None

    def default_reply_target(self) -> ReplyTarget | None:
        """Fallback ReplyTarget for spawns with no human origin (cron, watchdog,
        startup alerts). Returns the channel marked `is_alert_target`; falls
        back to the first registered channel if none is marked. Returns None
        if no bot has registered yet."""
        if not self._channels:
            return None
        # Prefer the explicit alert target.
        for channel, target_id, supports_files, machine_name, is_alert in self._channels:
            if is_alert and target_id:
                return ReplyTarget(
                    channel=channel, target_id=target_id,
                    supports_files=supports_files, machine_name=machine_name,
                )
        # No explicit flag → first channel with a non-empty target_id.
        for channel, target_id, supports_files, machine_name, _ in self._channels:
            if target_id:
                return ReplyTarget(
                    channel=channel, target_id=target_id,
                    supports_files=supports_files, machine_name=machine_name,
                )
        return None

    def _is_alive(self, worker_id: str) -> bool:
        for s in self.worker_state.snapshot():
            if s.worker_id == worker_id and s.alive:
                return True
        return False

    def _resolve_reply_to(self, reply_to: ReplyTarget | None) -> ReplyTarget | None:
        """Caller's origin (e.g. on_message ReplyTarget) wins; otherwise the
        dispatcher's default (first registered channel's alert)."""
        return reply_to if (reply_to and reply_to.is_valid) else self.default_reply_target()

    def _machine_name_for(self, reply_to: ReplyTarget | None) -> str:
        """Machine label for card titles. One global value per machine (config
        top-level `machine_name`)."""
        return self._config.machine_name

    def _machine_name(self) -> str:
        return self._config.machine_name

    def _reserve_worker(self, worker_id: str, label: str = "(starting)") -> bool:
        """Atomically reserve a worker for spawning. Returns False if it's
        already alive OR another spawn is mid-flight. Single-threaded asyncio
        guarantees this check + add is uninterruptible.

        Also marks the worker as alive in the tracker immediately (with pid=0
        as a placeholder) so `查状态` reflects the spawn during the 1-2s window
        between "task created" and "subprocess actually running" — otherwise
        the user sees "暂无 worker 状态" while their task is starting up."""
        if self._is_alive(worker_id) or worker_id in self._pending:
            return False
        self._pending.add(worker_id)
        self.worker_state.update_spawn(worker_id, label, pid=0)
        return True

    def _release_worker(self, worker_id: str) -> None:
        self._pending.discard(worker_id)

    def _on_spawn_done(
        self, task: asyncio.Task, worker_id: str, reply_to: ReplyTarget | None,
    ) -> None:
        """Inspect a finished spawn task. Three outcomes:
        - Task crashed (Python exception) → log + send error card.
        - Task exited code 2 (mcp-failed = extension not connected) →
          schedule auto-recovery (restart browser). User clicked "重启" but
          the real problem is the browser, not the worker process — heal it
          without making them figure that out.
        - Anything else → no callback action.
        """
        self._inflight.discard(task)
        self._release_worker(worker_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            import traceback
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(f"[spawn] {worker_id} task crashed:\n{tb}", flush=True)
            if reply_to and reply_to.is_valid:
                from agent.cards import error_card
                card = error_card(
                    f"[{self._machine_name_for(reply_to)}] {worker_id} spawn 崩溃",
                    f"`{type(exc).__name__}`: {str(exc)[:300]}",
                )
                asyncio.create_task(reply_to.send_card(card))
            return

        try:
            exit_code = task.result()
        except Exception:
            return
        if exit_code == 2 and worker_id in self._config.browsers:
            # mcp-failed + we know how to restart this worker's browser →
            # auto-recover. Schedule, don't await, since we're in a sync
            # callback context.
            asyncio.create_task(self._auto_recover_browser(worker_id, reply_to))

    async def _auto_recover_browser(self, worker_id: str, reply_to: ReplyTarget | None) -> None:
        """Triggered when a worker exits mcp-failed: kill + relaunch the
        browser to wake the extension. Sends progress cards so the user
        sees what's happening instead of just two error cards in a row."""
        from agent.cards import info_card, success_card
        machine = self._machine_name_for(reply_to)
        if reply_to and reply_to.is_valid:
            await reply_to.send_card(info_card(
                f"[{machine}] 自动救援 {worker_id} 浏览器",
                f"上一次任务退出码 2 (mcp-failed) — 扩展没连上。\n"
                f"正在重启 {worker_id} 的浏览器,完成后你可以再派任务。",
            ))
        try:
            card = await self.restart_browser_for(worker_id, reply_to)
        except Exception as exc:
            from agent.cards import error_card
            card = error_card(
                f"[{machine}] {worker_id} 浏览器自愈失败",
                f"`{type(exc).__name__}`: {str(exc)[:300]}\n手动检查浏览器/扩展状态。",
            )
        if reply_to and reply_to.is_valid:
            await reply_to.send_card(card)

    async def restart_worker(self, worker_id: str, reply_to: ReplyTarget | None = None) -> None:
        """Re-run the LAST skill this worker ran. "Restart" means "redo whatever
        it last did" — not "run some arbitrary default". If the worker has
        never run any skill, refuse cleanly instead of guessing fapiao-1688.

        If the worker is currently disconnected (extension not reachable),
        spawning a new task is pointless — it'll just exit code 2 in 2s.
        Route to restart_browser_for instead in that case.
        """
        from agent.cards import error_card, info_card
        wc = self._worker_config(worker_id)
        if wc is None:
            return
        machine = self._machine_name_for(reply_to)

        last_skill = self._current_label(worker_id)
        if not last_skill or last_skill == "?":
            if reply_to and reply_to.is_valid:
                await reply_to.send_card(error_card(
                    f"[{machine}] {worker_id} 不能重启",
                    f"`{worker_id}` 还没跑过任何 skill,没有「上次任务」可以重跑。\n"
                    f"请直接派一个任务,例如:`@bot {worker_id} 抓 1688 申请中发票`。",
                ))
            return

        # Quick connectivity check — if extension is down, restart_worker
        # is futile. Reroute to browser restart so user clicks ONE button
        # and the system figures out the right fix.
        connected = await self.probe_extension_connectivity(worker_id)
        if not connected:
            if reply_to and reply_to.is_valid:
                await reply_to.send_card(info_card(
                    f"[{machine}] {worker_id} 自愈中",
                    f"`{worker_id}` 当前不可用,改为重启浏览器(自动恢复)。完成后你可以再点重启。",
                ))
            card = await self.restart_browser_for(worker_id, reply_to)
            if reply_to and reply_to.is_valid:
                await reply_to.send_card(card)
            return
        # Probe succeeded — this worker is healthy. Clear stale unhealthy mark
        # if any (was set at startup but now extension is back).
        self._unhealthy.discard(worker_id)

        if not self._reserve_worker(worker_id, last_skill):
            print(f"[dispatch] {worker_id} already alive or pending, restart_worker skipped")
            return
        ts = _timestamp()
        log_path = self.log_dir / f"worker-{worker_id}-{ts}.log"
        resolved_reply = self._resolve_reply_to(reply_to)
        task = asyncio.create_task(
            spawn_one_skill(
                wc, last_skill, log_path, tracker=self.worker_state,
                reply_to=resolved_reply,
                machine_name=self._machine_name_for(resolved_reply),
                project_root=self._config.project_root,
            )
        )
        self._inflight.add(task)
        task.add_done_callback(
            lambda t, wid=worker_id, rt=resolved_reply: self._on_spawn_done(t, wid, rt)
        )

    async def spawn_now(
        self, worker_id: str, skill: str,
        reply_to: ReplyTarget | None = None,
        task: str = "",
    ) -> None:
        """`task`: optional free-form parameter forwarded to the skill as
        the worker's first user message. Used when a skill needs runtime
        input — e.g. ecom-best-source needs the JD product URL the user
        @-mentioned in the chat. Empty string means "no extra input"
        (the skill body is the full instruction)."""
        wc = self._worker_config(worker_id)
        if wc is None:
            return
        if worker_id in self._unhealthy:
            print(f"[health] {worker_id} is unhealthy, skipping spawn_now")
            return
        if not self._reserve_worker(worker_id, skill):
            print(f"[dispatch] {worker_id} already alive or pending (running {self._current_label(worker_id)!r}), spawn_now refused")
            return
        ts = _timestamp()
        log_path = self.log_dir / f"worker-{worker_id}-{ts}.log"
        resolved_reply = self._resolve_reply_to(reply_to)
        spawn_task = asyncio.create_task(
            spawn_one_skill(
                wc, skill, log_path, tracker=self.worker_state,
                reply_to=resolved_reply,
                machine_name=self._machine_name_for(resolved_reply),
                project_root=self._config.project_root,
                task=task,
            )
        )
        self._inflight.add(spawn_task)
        spawn_task.add_done_callback(
            lambda t, wid=worker_id, rt=resolved_reply: self._on_spawn_done(t, wid, rt)
        )

    async def spawn_freeform(self, worker_id: str, task: str, reply_to: ReplyTarget | None = None) -> None:
        wc = self._worker_config(worker_id)
        if wc is None:
            return
        if worker_id in self._unhealthy:
            print(f"[health] {worker_id} is unhealthy, skipping spawn_freeform")
            return
        freeform_label = f"freeform({task[:40]}{'...' if len(task) > 40 else ''})"
        if not self._reserve_worker(worker_id, freeform_label):
            print(f"[dispatch] {worker_id} already alive or pending, spawn_freeform refused")
            return
        ts = _timestamp()
        log_path = self.log_dir / f"worker-{worker_id}-freeform-{ts}.log"
        resolved_reply = self._resolve_reply_to(reply_to)
        t = asyncio.create_task(
            spawn_one_freeform(
                wc, task, log_path, tracker=self.worker_state,
                reply_to=resolved_reply,
                machine_name=self._machine_name_for(resolved_reply),
                project_root=self._config.project_root,
            )
        )
        self._inflight.add(t)
        t.add_done_callback(
            lambda x, wid=worker_id, rt=resolved_reply: self._on_spawn_done(x, wid, rt)
        )

    def _current_label(self, worker_id: str) -> str:
        for s in self.worker_state.snapshot():
            if s.worker_id == worker_id and s.last_skill:
                return s.last_skill
        return "?"

    async def set_paused(self, paused: bool) -> None:
        self._paused[0] = paused

    async def probe_extension_connectivity(self, worker_id: str) -> bool:
        """Real-time check: does this worker's bridge talk to the browser
        extension RIGHT NOW? Used by query_status to distinguish "⚪ 空闲"
        (bridge OK) from "🔴 断开连接" (bridge down).

        Uses `tabs_context_mcp` with **createIfEmpty=true** — the same call
        every skill makes at the start. Important side effect: it forces
        the extension's service worker to ensureTabGroup, which **wakes
        the service worker** if it's been suspended (Manifest V3 SWs go
        idle after 30s without events). So this probe doubles as a wake-up
        trigger, not just a passive check.

        Timeout bumped to 8s to give the SW time to spin up on cold path.
        Probes run in parallel across workers so query_status total
        latency stays bounded around 8s worst-case.

        Returns True only when the call succeeds AND its content doesn't
        contain "extension not connected". False on any error / timeout
        / extension-not-connected text.
        """
        wc = self._worker_config(worker_id)
        if wc is None:
            return False
        try:
            from agent.mcp_client import OpenClaudeInChromeClient
            async with asyncio.timeout(8):
                async with OpenClaudeInChromeClient(
                    port=wc.mcp_port, mcp_server_js_path=wc.mcp_server_js_path,
                ) as client:
                    result = await client.call_tool("tabs_context_mcp", {"createIfEmpty": True})
            text = "".join(
                (item.get("text", "") if isinstance(item, dict) else "")
                for item in (result.content or [])
            ).lower()
            if "extension is not connected" in text or "extension not connected" in text:
                return False
            return not result.is_error
        except Exception:
            return False

    async def restart_self(self, reply_to: ReplyTarget | None = None) -> dict:
        """Re-exec the master process itself.

        Refuses if any worker is alive OR if any spawn is mid-flight (would
        orphan a just-launching subprocess). Sends the reply card first, then
        schedules an `os.execv` ~1.5s later so the bot can deliver the message.
        After exec the process is replaced in-place — config.yaml is re-read,
        all loops restart.
        """
        from agent.cards import error_card, success_card

        machine_name = self._machine_name_for(reply_to)

        snapshot = self.worker_state.snapshot()
        alive_workers = [s.worker_id for s in snapshot if s.alive]
        # Pending = a spawn just requested but tracker not yet updated. Without
        # this check there's a window where restart_self runs between
        # create_task() and the subprocess actually launching.
        pending = sorted(self._pending - {s.worker_id for s in snapshot if s.alive})
        if alive_workers or pending:
            busy = sorted(set(alive_workers) | set(pending))
            return error_card(
                f"[{machine_name}] 重启 master 拒绝",
                f"以下 worker 还在跑或正要起来,会被孤立: {', '.join(busy)}。"
                f"先 `暂停所有` 并等它们退出。",
            )

        def _do_exec() -> None:
            import os
            import sys
            print(f"[{machine_name}] restart_self: execv → {sys.executable} {sys.argv}", flush=True)
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except OSError as exc:
                print(f"[{machine_name}] restart_self execv failed: {exc}", flush=True)
                os._exit(1)

        loop = asyncio.get_running_loop()
        loop.call_later(1.5, _do_exec)

        return success_card(
            f"[{machine_name}] master 即将重启",
            "1.5s 后 re-exec 主进程。config.yaml 会重新加载,所有 loop 重启。",
        )

    async def restart_browser_for(self, worker_id: str, reply_to: ReplyTarget | None = None) -> dict:
        # Defense-in-depth against concurrent invocations (e.g. Feishu retrying
        # the card.action event after a perceived timeout, or buggy event
        # subscription aliases). The 30s kill+launch cycle is NOT idempotent —
        # two parallel runs would kill each other's freshly-spawned Edge.
        if worker_id in self._restarting_browser:
            from agent.cards import warning_card
            return warning_card(
                f"[{self._machine_name_for(reply_to)}] {worker_id} 浏览器正在重启",
                f"已有一次重启进行中(约需 30s),忽略重复请求。",
            )
        self._restarting_browser.add(worker_id)
        try:
            return await self._restart_browser_for_impl(worker_id, reply_to)
        finally:
            self._restarting_browser.discard(worker_id)

    async def _restart_browser_for_impl(self, worker_id: str, reply_to: ReplyTarget | None = None) -> dict:
        """Restart the browser bound to worker_id (per config.browsers).

        Returns a card dict (delegates rendering to the caller's bot path).
        """
        from agent.browser_lifecycle import BrowserSpec, restart
        from agent.cards import error_card, success_card
        from agent.mcp_client import OpenClaudeInChromeClient

        machine_name = self._machine_name_for(reply_to)

        # Safety gate: refuse if any worker is alive (don't kill mid-task).
        snapshot = self.worker_state.snapshot()
        alive_workers = [s.worker_id for s in snapshot if s.alive]
        if alive_workers:
            return error_card(
                f"[{machine_name}] 重启浏览器拒绝",
                f"以下 worker 还在跑,先等它们空闲: {', '.join(alive_workers)}",
            )

        cfg = self._config.browsers.get(worker_id)
        if cfg is None:
            return error_card(
                f"[{machine_name}] {worker_id} 浏览器配置缺失",
                f"config.yaml 没在 `browsers:` 下配置 `{worker_id}` 的 executable",
            )

        spec = BrowserSpec(
            name=cfg.name,
            executable=cfg.executable,
            warmup_url=cfg.warmup_url,
        )
        result = await restart(spec)

        # Verify the EXTENSION actually connected after restart — not just
        # that the local mcp-server.js process answers. `list_tools()` is a
        # local query that returns even when the extension never connected,
        # which used to falsely report ✓ when the native messaging registry
        # entry was missing or the extension was disabled.
        #
        # The correct probe is `tabs_context_mcp(createIfEmpty=true)`: it
        # round-trips through native messaging into the extension's service
        # worker, so success implies (a) registry → host launched, (b) the
        # extension is enabled, and (c) connectNative succeeded. We poll for
        # up to ~20s because the extension's alarm-driven reconnect ticks
        # every ~24s after a cold browser launch.
        wc = self._worker_config(worker_id)
        bridge_ok = False
        if wc is not None and result.launch_ok:
            deadline = asyncio.get_event_loop().time() + 20
            attempt = 0
            while asyncio.get_event_loop().time() < deadline:
                attempt += 1
                try:
                    async with asyncio.timeout(6):
                        async with OpenClaudeInChromeClient(
                            port=wc.mcp_port, mcp_server_js_path=wc.mcp_server_js_path,
                        ) as client:
                            probe = await client.call_tool("tabs_context_mcp", {"createIfEmpty": True})
                    text = "".join(
                        (item.get("text", "") if isinstance(item, dict) else "")
                        for item in (probe.content or [])
                    ).lower()
                    if not probe.is_error and "extension is not connected" not in text and "extension not connected" not in text:
                        bridge_ok = True
                        break
                except Exception as exc:
                    print(f"[restart_browser] {worker_id} probe attempt {attempt} failed: {exc}")
                await asyncio.sleep(2)

        if not result.launch_ok:
            return error_card(
                f"[{machine_name}] {worker_id} 浏览器启动失败",
                f"原因:{result.reason}。请手动检查浏览器路径(config.yaml `browsers:` 块)。",
            )
        if not bridge_ok:
            return error_card(
                f"[{machine_name}] {worker_id} 浏览器起来了,但扩展没响应",
                f"浏览器进程已起,但扩展跟本机进程的通信测不通。\n"
                f"请检查:\n"
                f"- 扩展是否在 edge://extensions 里启用\n"
                f"- 启动时是否打开了 work.1688.com(扩展靠这个唤醒)\n"
                f"- (耗时 {result.elapsed_secs:.1f}s,杀了 {result.force_killed} 个进程)",
            )
        return success_card(
            f"[{machine_name}] {worker_id} 浏览器已重启",
            f"- 旧窗口已关闭({result.graceful_window_count} 个 main window)\n"
            f"- 兜底强杀({result.force_killed} 个残留进程)\n"
            f"- 新浏览器启动 + 扩展通信验证 ✓\n"
            f"- 耗时 {result.elapsed_secs:.1f}s",
        )

    def _worker_config(self, worker_id: str) -> WorkerConfig | None:
        for w in self._config.workers:
            if w.worker_id == worker_id:
                return w
        try:
            return worker_config_from_file(worker_id)
        except Exception:
            return None


async def spawn_one_skill(
    wc: WorkerConfig,
    skill: str,
    log_path: Path,
    dry_run: bool = False,
    tracker: WorkerStateTracker | None = None,
    reply_to: ReplyTarget | None = None,
    machine_name: str = "",
    project_root: Path = Path.cwd(),
    task: str = "",
) -> int:
    extra_args = ["--skill", skill]
    if task:
        extra_args += ["--task", task]
    return await _spawn_worker(
        wc, log_path, dry_run, tracker,
        extra_args=extra_args,
        label=skill,
        reply_to=reply_to,
        machine_name=machine_name,
        project_root=project_root,
    )


async def spawn_one_freeform(
    wc: WorkerConfig,
    task: str,
    log_path: Path,
    dry_run: bool = False,
    tracker: WorkerStateTracker | None = None,
    reply_to: ReplyTarget | None = None,
    machine_name: str = "",
    project_root: Path = Path.cwd(),
) -> int:
    label = f"freeform({task[:40]}{'...' if len(task) > 40 else ''})"
    return await _spawn_worker(
        wc, log_path, dry_run, tracker,
        extra_args=["--freeform", task],
        label=label,
        reply_to=reply_to,
        machine_name=machine_name,
        project_root=project_root,
    )


async def _notify_task_done(
    reply_to: ReplyTarget | None,
    machine_name: str,
    worker_id: str,
    label: str,
    exit_code: int,
    elapsed_s: float,
    output_files: list[Path] | None = None,
) -> None:
    """Push a task-completion card to the ReplyTarget the request originated
    from. If the channel supports files and the worker produced artifacts,
    upload each one too. Channel-agnostic — no Feishu / DingTalk / WeCom
    references in this function."""
    if reply_to is None or not reply_to.is_valid:
        return
    from agent.cards import error_card, success_card
    machine_label = machine_name or "machine"
    files = output_files or []
    if files:
        names = ", ".join(f"`{f.name}`" for f in files[:5])
        if len(files) > 5:
            names += f", … (+{len(files) - 5})"
        file_line = f"\n- 产出: {len(files)} 个文件 ({names})"
    else:
        file_line = ""
    if exit_code == 0:
        card = success_card(
            f"[{machine_label}] {worker_id} 任务完成",
            f"- 任务: `{label}`\n- 退出码: `0` (OK)\n- 耗时: {elapsed_s:.1f}s{file_line}",
        )
    else:
        status = _exit_code_label(exit_code)
        card = error_card(
            f"[{machine_label}] {worker_id} 任务失败",
            f"- 任务: `{label}`\n- 退出码: `{exit_code}` ({status})\n- 耗时: {elapsed_s:.1f}s{file_line}",
        )
    await reply_to.send_card(card)
    # Best-effort: upload artifacts if channel supports files.
    for f in files:
        await reply_to.send_file(f)


def _make_output_dir(project_root: Path, worker_id: str) -> Path:
    """Per-task output directory under <project_root>/outputs/.

    Pin to the configured project_root (derived from config.yaml's directory),
    NOT Path.cwd() — when master is started by systemd / Task Scheduler the cwd
    may be elsewhere. write_file's containment check uses the same project_root,
    so output_dir is always inside the allowed write area.
    """
    ts = _timestamp()
    out = project_root.resolve() / "outputs" / f"{worker_id}-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# Allowlist: only these extensions get auto-uploaded. Add more as needed,
# but keep the list small — channels can choke on huge or weird MIME types.
_UPLOAD_ALLOWED_SUFFIXES = frozenset({
    ".csv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
    ".json", ".md", ".txt", ".html",
})
# Feishu has DIFFERENT size caps per message type:
#   - 图片上传 (im.v1.image.create):  10 MB
#   - 文件上传 (im.v1.file.create):    30 MB
# Per-suffix cap so a 15 MB PNG doesn't pass our check then get rejected
# by Feishu. Conservative: stay under both caps by 5 MB.
_UPLOAD_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif"})
_UPLOAD_IMAGE_MAX_BYTES = 9 * 1024 * 1024     # 9 MB (image cap is 10)
_UPLOAD_FILE_MAX_BYTES = 25 * 1024 * 1024     # 25 MB (file cap is 30)


def _collect_outputs(output_dir: Path) -> list[Path]:
    """Files the worker wrote that are safe to upload back to the chat.

    Rules:
    - Non-recursive (only files directly in output_dir)
    - Skip hidden / tmp files (.tmp, leading-dot names)
    - Allowlist extensions: csv, xlsx, pdf, png, jpg, json, md, txt, html
    - Cap size at 25 MB

    Robust to TOCTOU: any OSError while listing/stat-ing falls through to
    "no outputs" rather than crashing — the completion card still goes out.
    """
    try:
        if not output_dir.is_dir():
            return []
        entries = sorted(output_dir.iterdir())
    except OSError as exc:
        print(f"[outputs] cannot list {output_dir}: {exc}", flush=True)
        return []
    safe: list[Path] = []
    for p in entries:
        try:
            if not p.is_file():
                continue
            name = p.name
            if name.startswith(".") or name.endswith(".tmp") or name.endswith(".partial"):
                continue
            if p.suffix.lower() not in _UPLOAD_ALLOWED_SUFFIXES:
                print(f"[outputs] skipping {name}: suffix {p.suffix!r} not in upload allowlist", flush=True)
                continue
            size = p.stat().st_size
        except OSError:
            continue
        cap = (
            _UPLOAD_IMAGE_MAX_BYTES
            if p.suffix.lower() in _UPLOAD_IMAGE_SUFFIXES
            else _UPLOAD_FILE_MAX_BYTES
        )
        if size > cap:
            print(f"[outputs] skipping {name}: {size} bytes > {cap} cap", flush=True)
            continue
        safe.append(p)
    return safe


async def _spawn_worker(
    wc: WorkerConfig,
    log_path: Path,
    dry_run: bool,
    tracker: WorkerStateTracker | None,
    extra_args: list[str],
    label: str,
    reply_to: ReplyTarget | None = None,
    machine_name: str = "",
    project_root: Path = Path.cwd(),
) -> int:
    if dry_run:
        print(f"[dry-run] would spawn {wc.worker_id} label={label} port={wc.mcp_port}")
        return 0

    started = datetime.now(tz=timezone.utc)
    output_dir = _make_output_dir(project_root, wc.worker_id)

    # Pass output dir + project root to worker via env. Worker's builtin
    # write_file uses WORKER_OUTPUT_DIR for relative paths and WORKER_PROJECT_ROOT
    # for the containment check (defends against Path.cwd() being something
    # else under systemd / Task Scheduler).
    import os
    env = dict(os.environ)
    env["WORKER_OUTPUT_DIR"] = str(output_dir)
    env["WORKER_PROJECT_ROOT"] = str(project_root.resolve())

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_fh:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent.worker",
            "--worker-id",
            wc.worker_id,
            *extra_args,
            "--port",
            str(wc.mcp_port),
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            cwd=str(project_root),
        )
        if tracker is not None:
            tracker.update_spawn(wc.worker_id, label, proc.pid)

        # Watchdog slider-alert needs (channel, chat_id) too; reuse reply_to.
        slider_channel = reply_to.channel if (reply_to and reply_to.is_valid) else None
        slider_chat_id = reply_to.target_id if (reply_to and reply_to.is_valid) else ""
        watchdog_tasks = _start_watchdogs(
            proc, wc, log_path, label, slider_channel, slider_chat_id,
        )

        exit_code = await proc.wait()

        for t in watchdog_tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        if tracker is not None:
            tracker.update_exit(wc.worker_id, exit_code)
        _log_worker_result(wc.worker_id, label, exit_code, log_path)

    elapsed_s = (datetime.now(tz=timezone.utc) - started).total_seconds()
    outputs = _collect_outputs(output_dir)
    await _notify_task_done(
        reply_to, machine_name,
        wc.worker_id, label, exit_code, elapsed_s,
        output_files=outputs,
    )
    return exit_code


def _max_idle_for_skill(skill: str, skills_dir: Path) -> int:
    try:
        from agent.skill_loader import SkillRegistry
        return SkillRegistry(skills_dir).load_full(skill).max_idle_minutes
    except Exception:
        return 10


def _start_watchdogs(
    proc: asyncio.subprocess.Process,
    wc: WorkerConfig,
    log_path: Path,
    skill: str,
    channel: Any | None,
    chat_id: str,
) -> list[asyncio.Task]:
    from agent.watchdog import idle_watchdog, slider_watchdog

    max_idle = _max_idle_for_skill(skill, wc.skills_dir)

    tasks: list[asyncio.Task] = []
    tasks.append(
        asyncio.create_task(
            idle_watchdog(proc, wc.worker_id, log_path, max_idle_minutes=max_idle),
            name=f"idle-watchdog-{wc.worker_id}",
        )
    )
    if channel is not None and chat_id:
        tasks.append(
            asyncio.create_task(
                slider_watchdog(proc, wc.worker_id, log_path, channel, chat_id, skill),
                name=f"slider-watchdog-{wc.worker_id}",
            )
        )
    return tasks


def _log_worker_result(worker_id: str, skill: str, exit_code: int, log_path: Path) -> None:
    ts = datetime.now(tz=timezone.utc).isoformat()
    status = "OK" if exit_code == 0 else _exit_code_label(exit_code)
    msg = f"[{ts}] worker={worker_id} skill={skill} exit={exit_code} ({status})"
    print(msg)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _exit_code_label(code: int) -> str:
    # User-facing labels — avoid jargon like "mcp". The user thinks in terms
    # of "browser", "extension", "skill", "model" — not MCP internals.
    labels = {
        1: "skill 执行失败",
        2: "浏览器扩展没连上",
        3: "模型(LLM)连接错误",
        4: "配置错误",
    }
    return labels.get(code, f"未知错误码 {code}")


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def fire_due_entries(
    store: ScheduleStore,
    config: MasterConfig,
    window_start: datetime,
    window_end: datetime,
    tracker: WorkerStateTracker | None = None,
    dry_run: bool = False,
    unhealthy: set[str] | None = None,
    dispatcher: "_MasterDispatcherImpl | None" = None,
) -> list[tuple[ScheduleEntry, int]]:
    due = store.find_due(window_start, window_end)
    results: list[tuple[ScheduleEntry, int]] = []
    if not due:
        return results

    # Dedup by worker: if two entries target b1 in the same 60s window,
    # fire only the first and skip the rest — otherwise two processes
    # would compete for the same browser/MCP port.
    seen_workers: set[str] = set()
    deduped: list[ScheduleEntry] = []
    for e in due:
        if e.worker in seen_workers:
            print(f"[dispatch] skipping schedule #{e.id} — {e.worker} already firing in this window")
            results.append((e, -3))
            continue
        seen_workers.add(e.worker)
        deduped.append(e)

    if not deduped:
        return results

    config.log_dir.mkdir(parents=True, exist_ok=True)
    _unhealthy = unhealthy or set()

    machine_name = config.machine_name
    fallback_reply = dispatcher.default_reply_target() if dispatcher else None

    def _reply_for_entry(entry: ScheduleEntry) -> ReplyTarget | None:
        """If schedule has origin (app_id+chat_id), route back to that
        specific (bot, chat). Otherwise fall back to default alert sink."""
        if dispatcher is None:
            return None
        if entry.origin_app_id and entry.origin_chat_id:
            origin_target = dispatcher.find_reply_target_by_app_id(
                entry.origin_app_id, entry.origin_chat_id,
            )
            if origin_target is not None:
                return origin_target
            # Origin bot/group went away (bot uninstalled / disabled) — fall back
            # so the user still sees the result somewhere, even if not the
            # original group.
            print(
                f"[dispatch] schedule #{entry.id} origin app_id={entry.origin_app_id!r} "
                f"chat_id={entry.origin_chat_id!r} not registered; falling back to alert sink"
            )
        return fallback_reply

    async def _fire(entry: ScheduleEntry) -> tuple[ScheduleEntry, int]:
        if entry.worker in _unhealthy:
            print(f"[health] skipping schedule #{entry.id} — {entry.worker} is unhealthy")
            return (entry, -2)
        wc = _resolve_worker(config, entry.worker)
        if wc is None:
            print(f"ALERT: schedule #{entry.id} references unknown worker {entry.worker}")
            return (entry, -1)
        if dispatcher is not None and not dry_run:
            if not dispatcher._reserve_worker(entry.worker, entry.skill):
                print(
                    f"[dispatch] schedule #{entry.id} {entry.worker} already alive/pending — skipping"
                )
                return (entry, -3)
        try:
            ts = _timestamp()
            log_path = config.log_dir / f"worker-{entry.worker}-{ts}.log"
            code = await spawn_one_skill(
                wc, entry.skill, log_path, dry_run=dry_run, tracker=tracker,
                reply_to=_reply_for_entry(entry), machine_name=machine_name,
                project_root=config.project_root,
            )
            return (entry, code)
        finally:
            if dispatcher is not None and not dry_run:
                dispatcher._release_worker(entry.worker)

    # Dispatcher present = live master loop: fire-and-forget so a 30-minute
    # 催开票 task doesn't stall the cron poll loop. The cron loop returns
    # immediately with placeholder code 0 = "scheduled"; real exit codes
    # appear later in the worker log and the task-done card.
    #
    # Dispatcher absent (run_once / tests) = synchronous gather so callers can
    # inspect the actual exit codes. Tests also rely on this.
    if dispatcher is None or dry_run:
        fired = await asyncio.gather(*[_fire(e) for e in deduped])
        results.extend(fired)
        return results

    for entry in deduped:
        t = asyncio.create_task(_fire(entry))
        dispatcher._inflight.add(t)
        t.add_done_callback(dispatcher._inflight.discard)
        results.append((entry, 0))
    return results


def _resolve_worker(config: MasterConfig, worker_id: str) -> WorkerConfig | None:
    for w in config.workers:
        if w.worker_id == worker_id:
            return w
    try:
        return worker_config_from_file(worker_id)
    except Exception:
        return None


async def _cron_loop(
    config: MasterConfig,
    store: ScheduleStore,
    dry_run: bool,
    tracker: WorkerStateTracker,
    paused: list[bool],
    unhealthy: set[str],
    dispatcher: _MasterDispatcherImpl | None = None,
) -> None:
    print(f"Master cron loop started. polling={CRON_POLL_INTERVAL_SECS}s")
    last_check = datetime.now(tz=timezone.utc)
    while True:
        await asyncio.sleep(CRON_POLL_INTERVAL_SECS)
        now = datetime.now(tz=timezone.utc)
        if paused[0]:
            print(f"Cron poll {now.isoformat()} — paused")
            last_check = now
            continue
        # Reload schedule.yaml each poll so entries added via @bot become
        # active without restarting master.
        try:
            store.reload()
        except Exception as exc:
            print(f"[cron] schedule reload failed: {exc}")
        results = await fire_due_entries(
            store, config, last_check, now,
            tracker=tracker, dry_run=dry_run,
            unhealthy=unhealthy, dispatcher=dispatcher,
        )
        for entry, code in results:
            label = "OK" if code == 0 else f"exit={code}"
            print(f"  fired #{entry.id} {entry.worker}:{entry.skill} → {label}")
        last_check = now


async def _bot_loop_one(
    bot_cfg,
    config: MasterConfig,
    dispatcher: _MasterDispatcherImpl,
) -> None:
    """Run a single bot. Dispatch by bot.type — only `feishu` is implemented now;
    `dingtalk` / `wecom` will be added later by adding cases here."""
    if not config.workers:
        return
    llm_settings = config.workers[0].llm_reasoning

    if bot_cfg.type == "feishu":
        from agent.bot import run_bot
        await run_bot(bot_cfg, llm_settings, dispatcher, machine_name=config.machine_name)
    else:
        # Should be caught at config-parse time, but defensive in case.
        print(f"[bot] unsupported bot type {bot_cfg.type!r}, skipping {bot_cfg.app_id}")


async def _log_rotation_loop(config: MasterConfig) -> None:
    """Once-per-day call to cleanup_old_logs (delete logs > 30 days, cap total at 1GB)."""
    from agent.log_rotation import cleanup_old_logs

    cleanup_old_logs(config.log_dir)  # run once on startup
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            cleanup_old_logs(config.log_dir)
        except Exception as exc:
            print(f"[log_rotation] cleanup failed: {exc}")


async def _knowledge_consolidation_loop(config: MasterConfig) -> None:
    """If this machine is the merger, run knowledge consolidation on cron."""
    from agent.knowledge_store import KnowledgeStore
    from agent.knowledge_sink import run_daily_consolidation
    from agent.llm_client import LLMClient
    from agent.scheduler import sleep_until_next_tick

    if not config.workers:
        return
    llm_settings = config.workers[0].llm_reasoning
    llm = LLMClient(
        base_url=llm_settings.base_url,
        api_key=llm_settings.api_key,
        model=llm_settings.model,
    )
    store = KnowledgeStore(config.knowledge.root)

    print(f"[knowledge] merger loop started. cron={config.knowledge.consolidate_cron!r}")
    while True:
        await sleep_until_next_tick(config.knowledge.consolidate_cron)
        try:
            results = await run_daily_consolidation(store, llm)
            updated = sum(1 for v in results.values() if v)
            print(f"[knowledge] consolidation done: {updated}/{len(results)} topics updated")
        except Exception as exc:
            print(f"[knowledge] consolidation failed: {exc}")


async def _primary_keepalive_loop(wc: WorkerConfig, log_dir: Path) -> None:
    """Keep a long-running mcp-server.js PRIMARY alive per worker.

    Architecture problem this solves:
    - Browser extension's native_messaging stub (native-host.js) tries to TCP
      connect to mcp_port (18765-18770). It needs SOMEONE listening on the
      other side.
    - Previously the only mcp-server.js instances were short-lived (per-task,
      via OpenClaudeInChromeClient async-with), so TCP port was UNBOUND
      between tasks → extension's 24s heartbeat retries kept failing →
      service worker eventually went stale → first task after idle failed.
    - Now: master spawns ONE long-running mcp-server.js per worker that
      stays in PRIMARY mode (owns the TCP port). Extension always connects
      successfully. Per-task worker spawns naturally enter CLIENT mode
      (mcp-server.js detects port taken and switches).

    Restart on crash with exponential backoff. Skips workers whose
    host/node_modules is missing (b1/b4/b5/b6 typically have no npm install)
    instead of spamming retry errors.
    """
    label = f"primary-{wc.worker_id}"

    if not wc.mcp_server_js_path.is_file():
        print(f"[{label}] mcp-server.js not found at {wc.mcp_server_js_path}, skipping")
        return
    node_modules = wc.mcp_server_js_path.parent / "node_modules"
    if not node_modules.is_dir():
        print(f"[{label}] node_modules missing — run `npm install` in {wc.mcp_server_js_path.parent}. Skipping.")
        return

    log_path = log_dir / f"mcp-primary-{wc.worker_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    backoff = 3
    proc: asyncio.subprocess.Process | None = None
    try:
        while True:
            try:
                env = dict(os.environ)
                env["OICC_PORT"] = str(wc.mcp_port)
                with log_path.open("ab") as log_fh:
                    log_fh.write(f"\n--- {label} spawn at {_timestamp()} port={wc.mcp_port} ---\n".encode("utf-8"))
                    log_fh.flush()
                    proc = await asyncio.create_subprocess_exec(
                        "node", str(wc.mcp_server_js_path),
                        env=env,
                        cwd=str(wc.mcp_server_js_path.parent),
                        stdin=asyncio.subprocess.PIPE,    # keep stdin open; mcp-server.js exits on EOF
                        stdout=log_fh,
                        stderr=log_fh,
                    )
                    print(f"[{label}] spawned pid={proc.pid} port={wc.mcp_port}", flush=True)
                    rc = await proc.wait()
                print(f"[{label}] exited rc={rc}, restarting in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[{label}] spawn error: {exc}, retrying in {backoff}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    except asyncio.CancelledError:
        # Master is shutting down — terminate the child cleanly.
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
            except Exception:
                pass
        raise


async def _ensure_cloned(target_dir: Path, repo_url: str, label: str) -> None:
    """If repo_url is set and target_dir is missing or empty, git clone it.
    No-op if already a git repo or no URL configured."""
    if not repo_url:
        return
    if (target_dir / ".git").is_dir():
        return
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.exists() and any(target_dir.iterdir()):
        print(f"[{label}] {target_dir} exists but is not a git repo and is non-empty. Skipping auto-clone.")
        return
    if target_dir.exists():
        target_dir.rmdir()
    print(f"[{label}] cloning {repo_url} → {target_dir} ...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_url, str(target_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            print(f"[{label}] clone OK")
        else:
            print(f"[{label}] clone failed: {stderr.decode('utf-8', errors='replace')[:300]}")
    except Exception as exc:
        print(f"[{label}] clone failed: {exc}")


async def _git_pull_loop(target_dir: Path, label: str, interval_secs: int) -> None:
    """Every `interval_secs`, git pull the target dir if it's a git repo."""
    if not (target_dir / ".git").is_dir():
        return
    print(f"[{label}] git auto-pull loop started. interval={interval_secs}s")
    while True:
        await asyncio.sleep(interval_secs)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(target_dir), "pull", "--ff-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0 and stdout:
                out = stdout.decode("utf-8", errors="replace").strip()
                if out and "Already up to date" not in out:
                    print(f"[{label}] git pull: {out[:200]}")
        except Exception as exc:
            print(f"[{label}] git pull failed: {exc}")


async def main_loop(config: MasterConfig, dry_run: bool = False) -> None:
    from agent.zombies import kill_zombie_oicc_processes
    from agent.health import run_health_checks, log_health_results

    kill_zombie_oicc_processes()

    tracker = WorkerStateTracker()
    paused: list[bool] = [False]
    store = ScheduleStore(config.project_root / SCHEDULE_STATE_PATH)
    unhealthy: set[str] = set()    # filled after health check; dispatcher holds the ref

    dispatcher = _MasterDispatcherImpl(
        config, tracker, store, paused,
        unhealthy=unhealthy,
    )

    skills_dir = config.skills.dir
    knowledge_dir = config.knowledge.root

    # Auto-clone if repo_url is set and local dir is missing.
    await _ensure_cloned(skills_dir, config.skills.repo_url, "skills")
    if config.knowledge.enabled:
        await _ensure_cloned(knowledge_dir, config.knowledge.repo_url, "knowledge")

    async with asyncio.TaskGroup() as tg:
        # PRIMARY keepalive — start FIRST so TCP ports 18765-18770 are bound
        # before the extension's 24s native_messaging heartbeat fires.
        # Workers that lack `npm install` (b1/b4/b5/b6 typically) are skipped
        # inside the loop, no error spam.
        for wc in config.workers:
            tg.create_task(
                _primary_keepalive_loop(wc, config.log_dir),
                name=f"primary-keepalive-{wc.worker_id}",
            )
        # Brief settle so PRIMARYs actually bind ports before health probes.
        await asyncio.sleep(2)

        # Health check NOW — workers find PRIMARY already listening, their
        # per-probe mcp-server.js enters CLIENT mode and talks to PRIMARY.
        # If extension isn't connected to PRIMARY yet, the probe will reflect
        # that and mark unhealthy.
        health_results = await run_health_checks(config.workers)
        unhealthy.update(log_health_results(health_results))

        tg.create_task(
            _cron_loop(config, store, dry_run, tracker, paused, unhealthy, dispatcher)
        )
        tg.create_task(_log_rotation_loop(config))
        tg.create_task(_git_pull_loop(skills_dir, "skills", config.skills.pull_interval_secs))
        if config.knowledge.enabled:
            tg.create_task(_git_pull_loop(knowledge_dir, "knowledge", config.knowledge.pull_interval_secs))
            if config.knowledge.is_merger:
                tg.create_task(_knowledge_consolidation_loop(config))
        for bot_cfg in config.active_bots():
            tg.create_task(_bot_loop_one(bot_cfg, config, dispatcher))


async def run_once(config: MasterConfig, dry_run: bool = False) -> None:
    tracker = WorkerStateTracker()
    store = ScheduleStore(config.project_root / SCHEDULE_STATE_PATH)
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(seconds=CRON_POLL_INTERVAL_SECS)
    print(f"--once window: {now.isoformat()} → {window_end.isoformat()}")
    results = await fire_due_entries(store, config, now, window_end, tracker=tracker, dry_run=dry_run)
    if not results:
        print("  no schedule entries due in this window")
    for entry, code in results:
        label = "OK" if code == 0 else f"exit={code}"
        print(f"  #{entry.id} {entry.worker}:{entry.skill} → {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 invoice automation master orchestrator")
    parser.add_argument("--once", action="store_true", help="fire any schedule entries due in the next minute, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print what would run without spawning")
    args = parser.parse_args()

    config = default_master_config()

    if args.once or args.dry_run:
        asyncio.run(run_once(config, dry_run=args.dry_run))
    else:
        asyncio.run(main_loop(config, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
