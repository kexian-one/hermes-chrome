from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent.llm_client import LLMClient
from agent.skill_loader import SkillRegistry


class Intent(str, Enum):
    QUERY_STATUS = "query_status"
    RESTART_WORKER = "restart_worker"
    QUERY_LOGS = "query_logs"
    QUERY_STATS = "query_stats"
    PAUSE_ALL = "pause_all"
    RESUME_ALL = "resume_all"
    HELP = "help"
    RUN_NOW = "run_now"
    SCHEDULE_ADD = "schedule_add"
    SCHEDULE_LIST = "schedule_list"
    SCHEDULE_REMOVE = "schedule_remove"
    SKILL_LIST = "skill_list"
    FREEFORM = "freeform"
    QUERY_KNOWLEDGE = "query_knowledge"
    UPDATE_SKILLS = "update_skills"
    RESTART_BROWSER = "restart_browser"
    RESTART_SELF = "restart_self"
    UNKNOWN = "unknown"
    ALERT_SLIDER = "alert_slider"  # master-internal: never routed by NLU


@dataclass
class IntentDispatch:
    intent: Intent
    args: dict


_SYSTEM_PROMPT_TEMPLATE = """You are an intent classifier for an all-in-ai automation bot.
Classify the user's message into exactly one of these intents and extract arguments.

Intents:
- query_status: User wants to know the current status of workers. Optional args: {"worker_id": "bN"} to limit to one worker. Examples:
    "现在啥情况" / "查状态" / "worker 都活着吗" / "正在跑什么" → {} (all workers)
    "查 b3 状态" / "看下 b2" / "b3 现在咋样" / "b2 状态" → {"worker_id": "b3"} / {"worker_id": "b2"}
- restart_worker: User wants to restart a worker. Args: {"worker_id": "bN"} (N is 1-6). Examples: "重启 worker 3" → {"worker_id": "b3"}, "b3 重启" → {"worker_id": "b3"}
- query_logs: User wants recent logs for a worker. Args: {"worker_id": "bN"}. Examples: "看 worker 3 日志", "b3 最近日志"
- query_stats: User wants today's statistics. Examples: "今天的统计", "今天开了多少"
- pause_all: User wants to pause all workers. Examples: "暂停所有", "停一下"
- resume_all: User wants to resume. Examples: "继续", "恢复"
- run_now: User wants to spawn a skill on a specific worker IMMEDIATELY (not on a schedule). Args: {"worker_id": "bN", "skill": "<skill-name>", "task": "<optional runtime input>"}. Map the user's natural language to one of the skills listed under "Available skills" below. If no skill matches, classify as unknown (do NOT invent a skill name).
    **task field**: When the user provides runtime input the skill needs (a URL, a SKU name, a filename, a topic), put that input — VERBATIM, in Chinese — into `task`. URLs go in `task` whole. If user gives no runtime input, omit `task` or set it to "". Examples below.
    Examples:
    "b1 现在跑 ecom-best-source https://item.jd.com/12345.html" → {"worker_id": "b1", "skill": "ecom-best-source", "task": "https://item.jd.com/12345.html"}
    "B1 找这个上游货源 https://b2b.jd.com/goods/goods-detail/10101356599310" → {"worker_id": "b1", "skill": "ecom-best-source", "task": "找这个上游货源 https://b2b.jd.com/goods/goods-detail/10101356599310"}    # matched by description
    "让 b2 比价这个京东链接 https://item.jd.com/12345.html 我要 232g" → {"worker_id": "b2", "skill": "ecom-best-source", "task": "比价这个京东链接 https://item.jd.com/12345.html 我要 232g"}    # matched by description
    "b2 去京东帮我刷优惠券" → unknown   # no matching skill, do NOT invent jd-coupons
    "b2 帮我找这个货源 https://b2b.jd.com/goods/goods-detail/10101356599310" →
        {"worker_id": "b2", "skill": "ecom-best-source", "task": "帮我找这个货源 https://b2b.jd.com/goods/goods-detail/10101356599310"}
    "b3 比下这个的价 https://item.jd.com/12345.html 我要的是原味70g" →
        {"worker_id": "b3", "skill": "ecom-best-source", "task": "比下这个的价 https://item.jd.com/12345.html 我要的是原味70g"}
- schedule_add: User wants to ADD a recurring scheduled task. Args: {"cron": "<5-field cron>", "worker_id": "bN", "skill": "<skill-name>"}. Same skill-matching rules as run_now.
    **HARD REQUIREMENT — only classify as schedule_add when the user message contains an EXPLICIT time / period word**:
    Trigger words (must appear in user text): 每天 / 每周 / 每月 / 每隔 / 每 N 小时 / 每 N 分钟 / 定时 / 周X / 早上 / 下午 / 晚上 / 点 (when referring to time, e.g. "16 点" "下午 3 点") / 分 (when time, e.g. "30 分") / at / daily / weekly / cron
    If NONE of these appear, the user is asking for IMMEDIATE execution — classify as `run_now`, NEVER as `schedule_add`. **Do not invent a cron value from thin air.**
    Convert natural-language times to standard cron only when the user gave one.
    Examples:
    "每天 16:00 让 b1 找货 https://item.jd.com/12345.html" → {"cron": "0 16 * * *", "worker_id": "b1", "skill": "ecom-best-source"} ✓ (has 每天 + 16:00)
    "每天早上 9 点让 b1 跑 ecom-best-source" → {"cron": "0 9 * * *", "worker_id": "b1", "skill": "ecom-best-source"} ✓
    "b1 找货 https://item.jd.com/12345.html" → run_now {"worker_id": "b1", "skill": "ecom-best-source", "task": "找货 https://item.jd.com/12345.html"}  ✗ NO time word → MUST be run_now
    "b1 现在跑 ecom-best-source" → run_now {...}  ✗ "现在" 是反指令 → run_now
- schedule_list: User wants to see all scheduled tasks. Examples: "看定时任务", "现在有哪些定时", "查 schedule", "定时任务列表"
- skill_list: User wants to see what SKILLS (capabilities) the bot currently has installed (not scheduled tasks). Examples: "你都能干啥", "有哪些 skill", "有什么技能", "看 skill 列表", "skills"
- schedule_remove: User wants to remove a scheduled task. Args: {"entry_id": <int>}. Examples: "删掉 #3" → {"entry_id": 3}, "删除定时 5" → {"entry_id": 5}
- freeform: FALLBACK — the user describes a concrete browser task on a worker, but NO existing skill matches it. The worker will receive the user's text verbatim and try to complete it using its MCP browser tools (navigate, click, read DOM, exec JS) without any pre-written skill body. Args: {"worker_id": "bN", "task": "<user's task verbatim, in Chinese>"}. ALWAYS prefer run_now / schedule_add with a matching skill if any matches. Only use freeform when you are sure no skill covers it. Examples:
    "b2 去京东帮我刷下购物车" → {"worker_id": "b2", "task": "去京东帮我刷下购物车"}
    "让 b3 在 1688 看下最近商家给我的留言" → {"worker_id": "b3", "task": "在 1688 看下最近商家给我的留言"}
- help: User asks for help. Examples: "你能做什么", "/help", "怎么用"
- query_knowledge: User wants to look up a knowledge topic from the local knowledge base. Args: {"topic": "<topic-or-fuzzy>"}. May be exact topic name or fuzzy natural-language. Examples:
    "查 knowledge 1688-shadow-dom" → {"topic": "1688-shadow-dom"}
    "关于滑块的知识" → {"topic": "滑块"}
    "看看 mtop 那块的笔记" → {"topic": "mtop"}
- update_skills: User wants to pull the latest skills from git (the skills/ directory is a git clone). Examples:
    "更新 skill" / "拉取 skill" / "skills 更新" / "git pull skills" / "更新一下技能"
- restart_browser: User wants to restart a worker's browser (kill + relaunch with warmup URL). Args: {"worker_id": "bN"}. Examples:
    "重启 b3 的浏览器" → {"worker_id": "b3"}
    "重新启动 b2 浏览器" → {"worker_id": "b2"}
    NOTE: this is DIFFERENT from restart_worker (which restarts the worker process, not the browser).
- restart_self: User wants the master/bot process ITSELF to restart (re-exec). No args. Use when user references "你自己", "master", "主进程", "bot 自己". Examples:
    "重启你自己" / "重启 master" / "重启主进程" / "你重启一下" / "重启 bot"
    NOTE: distinct from restart_worker (worker subprocess) and restart_browser (Chrome).
- unknown: None of the above match (e.g. greetings, off-topic, garbled text).

Respond with ONLY valid JSON in this exact format:
{"intent": "<intent_name>", "args": {...}}

Worker IDs are b1-b6. If the user says "worker 3", "b3", "账号 3", or just "3" (in a worker context), the worker_id is "b3".
Skill names are kebab-case: "ecom-best-source", etc. Use ONLY skill names that appear in the list below.

CONTEXT RECOVERY — when the user's CURRENT message is short, vague, or refers
to a prior task ("重试", "重试一下", "再试一次", "再来一遍", "改成 b3", "上面那个再跑一次"),
DO NOT classify as `unknown`. Instead:
1. Look at "Recent conversation" (if provided) AND "[用户引用了上一条消息]" (if present)
2. Find the most recent concrete instruction the user gave (e.g. "b1 找货 https://item.jd.com/12345.html")
3. Re-emit THAT intent with THOSE args. For "重试" alone, that usually means
   re-issuing the last `run_now` / `freeform` / `restart_worker` with the same parameters.
4. If the user is modifying ("改成 b3"), keep the same skill but update the changed field.
5. Only fall back to `unknown` when there's NO recoverable prior task in context.

Available skills on this machine:
{SKILLS_BLOCK}"""


_DEFAULT_SKILLS_DIR = Path("./skills")


def _format_skills_block(skills_dir: Path) -> str:
    if not skills_dir.is_dir():
        return "(none installed on this machine)"
    try:
        skills = SkillRegistry(skills_dir).list_skills()
    except Exception:
        return "(failed to load)"
    if not skills:
        return "(none installed on this machine)"
    lines = []
    for s in skills:
        desc = (s.description or "").replace("\n", " ").strip()
        if len(desc) > 200:
            desc = desc[:200] + "..."
        lines.append(f"- {s.name}: {desc}")
    return "\n".join(lines)


def _build_system_prompt(skills_dir: Path) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.replace("{SKILLS_BLOCK}", _format_skills_block(skills_dir))


logger = __import__("logging").getLogger(__name__)

# Greedy on purpose: payloads contain nested {...} (e.g. {"args": {}}).
# Non-greedy breaks on nesting. The "over-capture multiple JSON blocks"
# concern Codex raised is rarer than nested-JSON cases.
_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&'*+,;=%-]+")
_ECOM_URL_RE = re.compile(
    r"https?://(?:item\.jd\.com|b2b\.jd\.com|3\.cn)/[A-Za-z0-9._~:/?#@!$&'*+,;=%-]+",
    re.IGNORECASE,
)
_WORKER_ID_RE = re.compile(r"\bb([1-6])\b", re.IGNORECASE)


def _format_recent_context(recent_turns: list[tuple[str, str, str]]) -> str:
    """Render `(user_text, intent, summary)` triples for the NLU prompt.

    When intent/summary are empty (e.g. context fetched from Feishu chat
    history where we don't know how the bot classified those turns), omit
    the bot side entirely instead of printing `intent: , reply: ` which
    looks malformed and confuses the LLM."""
    lines = []
    for user_text, intent, summary in recent_turns:
        lines.append(f"  user: {user_text}")
        if intent or summary:
            lines.append(f"  bot intent: {intent}, reply: {summary}")
    return "\n".join(lines)


async def route(
    text: str,
    llm: LLMClient,
    skills_dir: Path | None = None,
    recent_turns: list[tuple[str, str, str]] | None = None,
) -> IntentDispatch:
    system_prompt = _build_system_prompt(skills_dir or _DEFAULT_SKILLS_DIR)
    user_content = text
    if recent_turns:
        context_block = _format_recent_context(recent_turns)
        user_content = f"Recent conversation:\n{context_block}\n\nCurrent message: {text}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        response = await llm.chat(messages)
        raw = (response.text or "").strip()
        data = _parse_json(raw)
        intent_str = data.get("intent", "unknown")
        try:
            intent = Intent(intent_str)
        except ValueError:
            intent = Intent.UNKNOWN
        args = data.get("args", {})
        if not isinstance(args, dict):
            args = {}
        dispatch = IntentDispatch(intent=intent, args=args)
        _recover_ecom_dispatch(dispatch, text)
        _recover_ecom_task_url(dispatch, text)
        return dispatch
    except Exception as exc:
        logger.warning("NLU route failed (returning unknown): %s: %s", type(exc).__name__, exc)
        return IntentDispatch(intent=Intent.UNKNOWN, args={})


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"intent": "unknown", "args": {}}


def _recover_ecom_task_url(dispatch: IntentDispatch, source_text: str) -> None:
    """Keep JD URLs in ecom tasks even when the classifier summarizes them away."""
    if dispatch.intent != Intent.RUN_NOW:
        return
    if dispatch.args.get("skill") != "ecom-best-source":
        return
    task = str(dispatch.args.get("task") or "").strip()
    if _ECOM_URL_RE.search(task) or _URL_RE.search(task):
        return
    match = _ECOM_URL_RE.search(source_text) or _URL_RE.search(source_text)
    if not match:
        return
    recovered_url = match.group(0)
    dispatch.args["task"] = f"{task} {recovered_url}".strip()


def _recover_ecom_dispatch(dispatch: IntentDispatch, source_text: str) -> None:
    if dispatch.intent not in {Intent.UNKNOWN, Intent.FREEFORM}:
        return
    if not _ECOM_URL_RE.search(source_text):
        return
    worker_match = _WORKER_ID_RE.search(source_text)
    worker_id = f"b{worker_match.group(1)}" if worker_match else str(dispatch.args.get("worker_id") or "b1")
    if not _WORKER_ID_RE.fullmatch(worker_id):
        worker_id = "b1"
    dispatch.intent = Intent.RUN_NOW
    dispatch.args = {
        "worker_id": worker_id.lower(),
        "skill": "ecom-best-source",
        "task": source_text.strip(),
    }
