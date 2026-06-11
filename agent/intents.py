from __future__ import annotations

import glob
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent.cards import (
    code_card,
    error_card,
    freeform_card_with_stop,
    info_card,
    schedule_list_card_with_actions,
    status_card_with_actions,
    success_card,
    warning_card,
)
from agent.channels import ReplyTarget
from agent.nlu import Intent, IntentDispatch
from agent.schedule_store import ScheduleStore
from agent.skill_loader import SkillRegistry
from agent.worker_state import WorkerStateTracker


class MasterDispatcher(Protocol):
    worker_state: WorkerStateTracker
    log_dir: Path
    schedule_store: ScheduleStore
    knowledge_root: Path
    skills_dir: Path
    # Worker ids that the user explicitly configured a browser for (config.browsers.*).
    # `query_status` shows these even if they've never been spawned, so the user
    # can see "I configured b3 but haven't run a task on it yet" instead of b3
    # being silently missing from the status table.
    configured_browser_ids: tuple[str, ...]

    async def restart_worker(self, worker_id: str, reply_to: ReplyTarget | None = None) -> None: ...
    async def spawn_now(
        self, worker_id: str, skill: str,
        reply_to: ReplyTarget | None = None,
        task: str = "",
    ) -> None: ...
    async def spawn_freeform(self, worker_id: str, task: str, reply_to: ReplyTarget | None = None) -> None: ...
    async def restart_browser_for(self, worker_id: str, reply_to: ReplyTarget | None = None) -> dict: ...
    async def restart_self(self, reply_to: ReplyTarget | None = None) -> dict: ...
    async def set_paused(self, paused: bool) -> None: ...
    # Real-time check: does this worker's mcp-server.js bridge talk to the
    # browser extension successfully RIGHT NOW? Returns True/False.
    # `query_status` calls this for each configured worker to show
    # "🔴 断开连接" vs "⚪ 空闲" instead of conflating them.
    async def probe_extension_connectivity(self, worker_id: str) -> bool: ...


_WORKER_RE = re.compile(r"^b[1-6]$")
_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


async def handle(
    dispatch: IntentDispatch,
    master: MasterDispatcher,
    machine_name: str,
    reply_to: ReplyTarget | None = None,
) -> dict:
    """Dispatch a parsed intent. `reply_to` carries the origin (which bot /
    which chat) so worker completion notifications route back to the same
    channel — never cross-channel. Cron-fired tasks pass reply_to=None and
    the master's default channel is used."""
    match dispatch.intent:
        case Intent.QUERY_STATUS:
            return await _query_status(dispatch.args, master, machine_name)
        case Intent.RESTART_WORKER:
            return await _restart_worker(dispatch.args, master, machine_name, reply_to)
        case Intent.QUERY_LOGS:
            return _query_logs(dispatch.args, master, machine_name)
        case Intent.QUERY_STATS:
            return _query_stats(master, machine_name)
        case Intent.PAUSE_ALL:
            return await _pause_all(master, machine_name)
        case Intent.RESUME_ALL:
            return await _resume_all(master, machine_name)
        case Intent.RUN_NOW:
            return await _run_now(dispatch.args, master, machine_name, reply_to)
        case Intent.SCHEDULE_ADD:
            return _schedule_add(dispatch.args, master, machine_name, reply_to)
        case Intent.SCHEDULE_LIST:
            return _schedule_list(master, machine_name)
        case Intent.SCHEDULE_REMOVE:
            return _schedule_remove(dispatch.args, master, machine_name)
        case Intent.SKILL_LIST:
            return _skill_list(master, machine_name)
        case Intent.FREEFORM:
            return await _freeform(dispatch.args, master, machine_name, reply_to)
        case Intent.QUERY_KNOWLEDGE:
            return _query_knowledge(dispatch.args, master, machine_name)
        case Intent.UPDATE_SKILLS:
            return await _update_skills(master, machine_name)
        case Intent.RESTART_BROWSER:
            return await _restart_browser(dispatch.args, master, machine_name, reply_to)
        case Intent.RESTART_SELF:
            return await master.restart_self(reply_to)
        case Intent.HELP:
            return _help(master, machine_name)
        case _:
            return warning_card(
                f"[{machine_name}] 没听懂",
                "试试 `/help` 看支持的指令。",
            )


async def _query_status(args: dict, master: MasterDispatcher, machine_name: str) -> dict:
    import asyncio as _asyncio

    # Show union of (workers spawned at least once) and (workers configured
    # in config.browsers). Spawned-only covers "I ran this already"; the
    # configured set ensures a configured-but-never-run b3 still appears so
    # the user can see "yes I configured it, but extension is down".
    states_by_id = {s.worker_id: s for s in master.worker_state.snapshot()}
    configured_ids = set(getattr(master, "configured_browser_ids", ()) or ())
    all_ids = sorted(set(states_by_id) | configured_ids)

    # Optional filter: NLU may extract worker_id from "查 b3 状态" / "看下 b2".
    # When present, show only that one row. Unknown id → just show it as
    # 🔴 断开连接 (the probe below will reflect reality).
    requested = args.get("worker_id", "") if isinstance(args, dict) else ""
    if requested and _WORKER_RE.match(requested):
        all_ids = [requested]

    if not all_ids:
        return info_card(
            f"[{machine_name}] Worker 状态",
            "暂无 worker 状态信息(还没配 browsers、也没跑过任何任务)。",
        )

    # Probe extension connectivity for workers that are NOT currently running
    # a task — running workers obviously have the bridge alive, no need to
    # waste a probe on them. Probes run in parallel via gather.
    probe_targets = [
        wid for wid in all_ids
        if not (states_by_id.get(wid) and states_by_id[wid].alive)
    ]
    probe_results: dict[str, bool] = {}
    if probe_targets and hasattr(master, "probe_extension_connectivity"):
        results = await _asyncio.gather(
            *[master.probe_extension_connectivity(w) for w in probe_targets],
            return_exceptions=True,
        )
        for wid, r in zip(probe_targets, results):
            probe_results[wid] = (r is True)

    lines = ["| Worker | 状态 |", "| --- | --- |"]
    for wid in all_ids:
        s = states_by_id.get(wid)
        if s and s.alive:
            status = "🟢 运行中"
        elif probe_results.get(wid, False):
            status = "⚪ 空闲"          # 扩展通,只是没在跑任务
        else:
            status = "🔴 断开连接"      # 扩展不通(浏览器关了 / 扩展睡了 / 没装 npm 依赖)
        lines.append(f"| `{wid}` | {status} |")

    # Single-worker view: keep [重启] [看日志] action buttons — only 2 buttons,
    # makes follow-up actions one tap away.
    # Multi-worker view: no buttons — 12 buttons (2×6) would clutter the card.
    # User can still type "重启 bN" / "看 bN 日志" in chat.
    if len(all_ids) == 1:
        return status_card_with_actions(
            f"[{machine_name}] Worker 状态",
            "\n".join(lines),
            all_ids,
        )
    return info_card(
        f"[{machine_name}] Worker 状态",
        "\n".join(lines),
    )


async def _restart_worker(
    args: dict, master: MasterDispatcher, machine_name: str, reply_to: ReplyTarget | None,
) -> dict:
    worker_id = args.get("worker_id", "")
    if not _WORKER_RE.match(worker_id):
        return error_card(
            f"[{machine_name}] 参数错误",
            "请指定 worker 编号(b1-b6),例如:`重启 b3`",
        )
    await master.restart_worker(worker_id, reply_to)
    return success_card(f"[{machine_name}] 已触发重启", f"`{worker_id}` 重启请求已派发。")


def _query_logs(args: dict, master: MasterDispatcher, machine_name: str) -> dict:
    worker_id = args.get("worker_id", "")
    if not _WORKER_RE.match(worker_id):
        return error_card(
            f"[{machine_name}] 参数错误",
            "请指定 worker 编号(b1-b6),例如:`看 b3 日志`",
        )

    pattern = str(master.log_dir / f"worker-{worker_id}-*.log")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return info_card(
            f"[{machine_name}] {worker_id} 日志",
            f"未找到 `{worker_id}` 的日志文件。",
        )

    log_path = Path(matches[-1])
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-50:] if len(lines) > 50 else lines
        content = "\n".join(tail) if tail else "(日志为空)"
    except OSError as e:
        return error_card(f"[{machine_name}] 读日志失败", str(e))

    return code_card(
        f"[{machine_name}] {worker_id} 最近日志 ({log_path.name})",
        content,
    )


def _query_stats(master: MasterDispatcher, machine_name: str) -> dict:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    pattern = str(master.log_dir / f"worker-b*-{today}*.log")
    log_files = sorted(glob.glob(pattern))

    if not log_files:
        today_local = datetime.now().strftime("%Y%m%d")
        pattern2 = str(master.log_dir / f"worker-b*-{today_local}*.log")
        log_files = sorted(glob.glob(pattern2))

    if not log_files:
        return info_card(f"[{machine_name}] 今日统计", f"今天({today})暂无日志文件。")

    total_ok = 0
    total_fail = 0
    for lf in log_files:
        try:
            text = Path(lf).read_text(encoding="utf-8", errors="replace")
            total_ok += text.count("exit=0 (OK)")
            total_fail += sum(1 for line in text.splitlines() if "exit=" in line and "exit=0" not in line)
        except OSError:
            pass

    body = (
        f"| 指标 | 值 |\n"
        f"| --- | --- |\n"
        f"| 日志文件数 | {len(log_files)} |\n"
        f"| 成功任务数 | **{total_ok}** |\n"
        f"| 失败任务数 | **{total_fail}** |"
    )
    return info_card(f"[{machine_name}] 今日统计", body)


async def _pause_all(master: MasterDispatcher, machine_name: str) -> dict:
    await master.set_paused(True)
    return warning_card(
        f"[{machine_name}] 已暂停调度",
        "下次 cron 不触发新任务。发`继续`恢复。",
    )


async def _resume_all(master: MasterDispatcher, machine_name: str) -> dict:
    await master.set_paused(False)
    return success_card(f"[{machine_name}] 已恢复调度", "")


async def _run_now(
    args: dict, master: MasterDispatcher, machine_name: str, reply_to: ReplyTarget | None,
) -> dict:
    worker_id = args.get("worker_id", "")
    skill = args.get("skill", "")
    task = str(args.get("task", "") or "").strip()
    if not _WORKER_RE.match(worker_id):
        return error_card(f"[{machine_name}] 参数错误", "worker_id 需要 b1-b6")
    if not _SKILL_RE.match(skill):
        return error_card(f"[{machine_name}] 参数错误", "skill 名不对,例如 `fapiao-1688`")
    await master.spawn_now(worker_id, skill, reply_to, task=task)
    task_hint = f" · 输入: {task[:60]}{'...' if len(task) > 60 else ''}" if task else ""
    return success_card(
        f"[{machine_name}] 已派发",
        f"`{worker_id}` 正在跑 `{skill}`{task_hint}",
    )


def _schedule_add(
    args: dict, master: MasterDispatcher, machine_name: str,
    reply_to: ReplyTarget | None = None,
) -> dict:
    cron = str(args.get("cron", ""))
    worker_id = args.get("worker_id", "")
    skill = args.get("skill", "")
    created_by = args.get("created_by", "")

    if not cron:
        return error_card(f"[{machine_name}] 参数错误", "缺少 cron 表达式")
    if not _WORKER_RE.match(worker_id):
        return error_card(f"[{machine_name}] 参数错误", "worker_id 需要 b1-b6")
    if not _SKILL_RE.match(skill):
        return error_card(f"[{machine_name}] 参数错误", "skill 名不对")

    # Origin tracking — cron firings will reply to whichever (bot, group) the
    # schedule was created in. If reply_to is absent (e.g. invoked from CLI),
    # falls back to alert_chat_id at fire time.
    origin_app_id = ""
    origin_chat_id = ""
    if reply_to and reply_to.is_valid:
        origin_chat_id = reply_to.target_id
        # Channel handle is duck-typed; only Feishu's FeishuChannel exposes app_id.
        # If not exposed, we still record origin_chat_id and fire_due_entries
        # uses the chat_id to find a matching channel.
        origin_app_id = getattr(reply_to.channel, "app_id", "") or ""

    try:
        entry = master.schedule_store.add(
            cron, worker_id, skill, created_by,
            origin_app_id=origin_app_id, origin_chat_id=origin_chat_id,
        )
    except ValueError as e:
        return error_card(f"[{machine_name}] cron 表达式无效", str(e))

    return success_card(
        f"[{machine_name}] 已添加定时 #{entry.id}",
        f"- cron: `{entry.cron}`\n- worker: `{entry.worker}`\n- skill: `{entry.skill}`",
    )


def _schedule_list(master: MasterDispatcher, machine_name: str) -> dict:
    entries = master.schedule_store.list_all()
    if not entries:
        return info_card(
            f"[{machine_name}] 定时任务",
            "没有定时任务。用 `每天 16:00 让 b1 找货 https://item.jd.com/12345.html` 添加。",
        )

    lines = ["| # | cron | worker | skill | 状态 |", "| --- | --- | --- | --- | --- |"]
    for e in entries:
        status = "🟢" if e.enabled else "⚪ disabled"
        lines.append(f"| #{e.id} | `{e.cron}` | `{e.worker}` | `{e.skill}` | {status} |")
    entry_ids = [e.id for e in entries]
    return schedule_list_card_with_actions(
        f"[{machine_name}] 定时任务 ({len(entries)} 条)",
        "\n".join(lines),
        entry_ids,
    )


def _schedule_remove(args: dict, master: MasterDispatcher, machine_name: str) -> dict:
    raw_id = args.get("entry_id", args.get("id", 0))
    try:
        entry_id = int(raw_id)
    except (TypeError, ValueError):
        return error_card(f"[{machine_name}] 参数错误", "请提供数字 ID,例如 `删掉 #3`")

    if master.schedule_store.remove(entry_id):
        return success_card(f"[{machine_name}] 已删除 #{entry_id}", "")
    return warning_card(
        f"[{machine_name}] 没找到 #{entry_id}",
        "用 `看定时任务` 查看现有列表。",
    )


def _skill_list(master: MasterDispatcher, machine_name: str) -> dict:
    skills_dir = master.skills_dir
    if not skills_dir.is_dir():
        return info_card(f"[{machine_name}] 已装技能", f"({skills_dir} 目录不存在)")
    try:
        skills = SkillRegistry(skills_dir).list_skills()
    except Exception as e:
        return error_card(f"[{machine_name}] 读 skills 失败", str(e))
    if not skills:
        return info_card(f"[{machine_name}] 已装技能", "(还没安装任何 skill)")

    lines = []
    for s in skills:
        desc = (s.description or "").replace("\n", " ").strip()
        if len(desc) > 150:
            desc = desc[:150] + "…"
        lines.append(f"- **`{s.name}`** — {desc}")
    return info_card(
        f"[{machine_name}] 已装技能 ({len(skills)} 个)",
        "\n".join(lines),
    )


async def _freeform(
    args: dict, master: MasterDispatcher, machine_name: str, reply_to: ReplyTarget | None,
) -> dict:
    worker_id = args.get("worker_id", "")
    task = str(args.get("task", "")).strip()
    if not _WORKER_RE.match(worker_id):
        return error_card(f"[{machine_name}] 参数错误", "worker_id 需要 b1-b6")
    if not task:
        return error_card(f"[{machine_name}] 参数错误", "缺少任务描述")
    await master.spawn_freeform(worker_id, task, reply_to)
    body = (
        f"`{worker_id}` 收到临时任务:\n\n> {task}\n\n"
        "⚠ freeform 没有预写 SKILL.md,worker 会自行规划步骤。"
        "速度慢、成败不稳定。频繁要做的事建议固化成 SKILL.md。"
    )
    return freeform_card_with_stop(
        f"[{machine_name}] 已派发 (freeform 模式)",
        body,
        worker_id,
    )


def _query_knowledge(args: dict, master: MasterDispatcher, machine_name: str) -> dict:
    import difflib
    from agent.knowledge_store import KnowledgeStore

    topic_query = str(args.get("topic", "")).strip()
    if not topic_query:
        return error_card(f"[{machine_name}] 参数错误", "请指定 knowledge 主题")

    store = KnowledgeStore(root=master.knowledge_root)
    all_topics = store.list_topics()
    if not all_topics:
        return info_card(
            f"[{machine_name}] knowledge: {topic_query}",
            "knowledge 库还是空的(没有任何条目)。worker 学到东西会自动 append。",
        )

    # exact hit
    content = store.load_curated(topic_query)
    matched_topic = topic_query

    if content is None:
        # fuzzy: substring + difflib close matches
        candidates = [t for t in all_topics if topic_query.lower() in t.lower()]
        if not candidates:
            candidates = difflib.get_close_matches(topic_query, all_topics, n=3, cutoff=0.5)

        if not candidates:
            return warning_card(
                f"[{machine_name}] 没找到 knowledge: {topic_query}",
                f"现有 topic({len(all_topics)} 个):\n" + "\n".join(f"- `{t}`" for t in all_topics[:20]),
            )

        if len(candidates) > 1:
            return info_card(
                f"[{machine_name}] 多个 topic 匹配 '{topic_query}'",
                "你是说这几个之一吗?明确一下:\n" + "\n".join(f"- `{t}`" for t in candidates),
            )

        matched_topic = candidates[0]
        content = store.load_curated(matched_topic)

        if content is None:
            # fallback: stitch by-machine views
            views = store.list_machine_views(matched_topic)
            if not views:
                return warning_card(
                    f"[{machine_name}] knowledge: {matched_topic}",
                    "topic 存在但当前 curated/by-machine 都读不到内容。",
                )
            parts = [f"### {mid}\n\n{c}" for mid, c in views.items()]
            content = "*(尚未合并 — 显示各机原始版本)*\n\n" + "\n\n---\n\n".join(parts)

    return info_card(
        f"[{machine_name}] knowledge: {matched_topic}",
        content,
    )


async def _update_skills(master: MasterDispatcher, machine_name: str) -> dict:
    import asyncio as _asyncio

    skills_dir = master.skills_dir
    if not (skills_dir / ".git").is_dir():
        return error_card(
            f"[{machine_name}] update_skills 失败",
            f"`{skills_dir}` 不是 git 仓库。先 `git clone <repo> skills/` 一次。",
        )

    try:
        proc = await _asyncio.create_subprocess_exec(
            "git", "-C", str(skills_dir), "pull", "--ff-only",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await _asyncio.wait_for(proc.communicate(), timeout=30)
        except _asyncio.TimeoutError:
            proc.kill()
            try:
                await _asyncio.wait_for(proc.wait(), timeout=2)
            except _asyncio.TimeoutError:
                pass
            return error_card(
                f"[{machine_name}] update_skills 超时",
                "30 秒内 git pull 没返回。可能在等凭据或网络问题。",
            )
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
    except FileNotFoundError:
        return error_card(f"[{machine_name}] update_skills 失败", "找不到 git 可执行,请安装 git")
    except Exception as exc:
        return error_card(f"[{machine_name}] update_skills 失败", str(exc))

    if proc.returncode != 0:
        return error_card(
            f"[{machine_name}] git pull 失败 (exit={proc.returncode})",
            f"```\n{stderr or stdout}\n```",
        )

    new_skills = SkillRegistry(skills_dir).list_skills()
    output = stdout or "(no output)"
    return success_card(
        f"[{machine_name}] skills 已更新",
        f"git pull:\n```\n{output}\n```\n\n现在共 **{len(new_skills)}** 个 skill",
    )


async def _restart_browser(
    args: dict, master: MasterDispatcher, machine_name: str, reply_to: ReplyTarget | None,
) -> dict:
    worker_id = args.get("worker_id", "")
    if not _WORKER_RE.match(worker_id):
        return error_card(
            f"[{machine_name}] 参数错误",
            "请指定 worker 编号(b1-b6),例如:`重启 b3 浏览器`",
        )
    return await master.restart_browser_for(worker_id, reply_to)


def _active_skill_names(master: MasterDispatcher) -> list[str]:
    try:
        if not master.skills_dir.is_dir():
            return []
        return [s.name for s in SkillRegistry(master.skills_dir).list_skills()]
    except Exception:
        return []


def _help_examples_for_skills(skill_names: list[str]) -> str:
    if "ecom-best-source" in skill_names:
        return """\
**立即执行**
- `b1 找货 https://b2b.jd.com/goods/goods-detail/10128813484820` — 根据京东/京东万商链接找 1688 上游货源
- `b1 比价 https://item.jd.com/12345.html 我要 232g` — 带 SKU/规格要求找同款
- `b1 现在跑 ecom-best-source https://item.jd.com/12345.html` — 跑指定 skill(精确名)
- `重启 b1` — 重启 worker 进程
- `重启你自己` / `重启 master` — 重启 master 主进程(re-exec,需所有 worker 空闲)

**定时管理**
- `每天 16:00 让 b1 找货 https://item.jd.com/12345.html` — 添加定时
- `删掉 #3` — 删除编号 3 的定时"""
    if skill_names:
        first = skill_names[0]
        return f"""\
**立即执行**
- `b1 现在跑 {first}` — 跑指定 skill(精确名)
- `b1 描述你要做的任务` — 描述任务,自动匹配已安装 skill
- `重启 b1` — 重启 worker 进程
- `重启你自己` / `重启 master` — 重启 master 主进程(re-exec,需所有 worker 空闲)

**定时管理**
- `每天 16:00 让 b1 跑 {first}` — 添加定时
- `删掉 #3` — 删除编号 3 的定时"""
    return """\
**立即执行**
- 当前没有启用的 skill。先用 `看 skill 列表` 确认技能目录。
- `重启你自己` / `重启 master` — 重启 master 主进程(re-exec,需所有 worker 空闲)

**定时管理**
- `看定时任务` — 列出所有定时
- `删掉 #3` — 删除编号 3 的定时"""


def _help(master: MasterDispatcher, machine_name: str) -> dict:
    skill_names = _active_skill_names(master)
    skill_line = (
        ", ".join(f"`{name}`" for name in skill_names)
        if skill_names else "(无启用 skill)"
    )
    body = f"""\
**查询类**
- `查状态` — 列出 worker 当前状态
- `看 b3 日志` — 查看 worker 最近 50 行
- `今天的统计` — 统计今日成功/失败
- `看定时任务` — 列出所有定时
- `看 skill 列表` — 已安装的技能
- `查 knowledge XX` — 查 knowledge 主题(支持模糊匹配)
- `/help` — 本帮助

**当前启用 skill**
{skill_line}

{_help_examples_for_skills(skill_names)}

**维护**
- `更新 skill` — git pull 拉新 skill
- `暂停所有` / `继续` — toggle 全局调度"""
    return info_card(f"[{machine_name}] 指令帮助", body)
