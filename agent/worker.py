from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from agent.builtin_tools import BUILTIN_TOOLS, execute_builtin, is_builtin
from agent.config import WorkerConfig, worker_config_from_file
from agent.llm_client import LLMClient
from agent.mcp_client import OpenClaudeInChromeClient, tools_to_openai_functions
from agent.skill_loader import SkillRegistry

# Hard limit on LLM ↔ tool round-trips per worker run.
# Sizing rule of thumb: chase one merchant end-to-end takes ~7-8 iterations
# after SKILL.md optimization (set qInput / find search coord / click search /
# find 旺旺 coord / click 旺旺 / find new IM tab / batch JS send + close).
# 400 covers ~50 merchants per single worker run with ~50 step buffer.
# Beyond ~50 merchants: LLM context bloat becomes the real bottleneck
# (not iteration count) — at that scale move to per-batch worker spawn,
# not just raising this cap further.
MAX_ITERATIONS = 400
_ECOM_REQUIRED_OUTPUT_RETRIES = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("worker")


_WORKER_OPERATOR_GUIDANCE = """\
## 操作原则(适用于所有 skill)

你有完整的浏览器操作工具(包括 `computer` 鼠标点击/拖拽/截图)和文本工具
(`javascript_tool` / `read_page` / `find` / `get_page_text`)。**你自己判断**用
什么。

**优先级**:
1. **遇到拿不准的页面状态时,先截图看一眼** —— `computer` 工具截屏,你
   能直接"看"到屏幕,比脑补 DOM 高效。**但截图代价高(每张需要约 30 秒
   多模态描述)** —— 节俭使用,只在以下时机拍:
   - 进入新页面 / 新 tab 第一次(必拍,获取布局)
   - 关键决策点之前(比如点哪个按钮才对)
   - **不要为了"确认刚才的 click / type 成功"再截图** — 信任 action
     工具的返回,如果 type 后页面没反应,再拍
   - 一次截图拿到的多模态描述够用就别重拍 — 同一个页面状态描述不变,
     除非你刚做了改动它的事(click/type/navigate)
2. **意外弹窗 / 提示 / 风控页**:**自己处理**。判定原则:
   - 有 × 关闭按钮(或"忽略"/"以后再说") → 自己关掉继续,**不通知用户**
   - 需要拖滑块 / 输验证码 / 重新登录 / 整页空白没头绪 → 才升级给用户
3. **DOM 选择器写不对、找不到元素**:不要硬撞 N 次。截图看屏幕实际样子,
   或用 `computer` 鼠标在视觉坐标上点击。
   - **看到多模态描述给的坐标,优先直接 `computer.left_click` 用,不要
     再花一堆 javascript_tool 调用去验证 / 再找 DOM 选择器**。1688 用了
     大量 closed shadow root,DOM 选择器经常穿不透;multimodal 看到的就是
     用户眼睛看到的,坐标可信。
4. **重复失败 3 次**:换思路(不同 selector / 不同入口 / 截图回看),
   不要无限重试。
5. **任务无法完成**(登录态丢 / 严重风控 / 页面变化太大):停下,写清楚
   原因 + 当前进度,让用户看到状态。

**用户偏好**:不要为了每个小弹窗都打断用户。能自己处理的统统处理。"""


FREEFORM_SYSTEM_PROMPT = _WORKER_OPERATOR_GUIDANCE + """

## Freeform 模式(没有预写 SKILL.md)

你是一个浏览器自动化 agent,通过 MCP 工具操作 Chromium 浏览器完成用户
描述的任务。

可用工具(运行时已注入):
- tabs_context_mcp / tabs_create_mcp:获取或新建浏览器 tab
- navigate:跳转 URL
- javascript_tool / get_page_text / read_page / find:探查页面内容
- form_input / shortcuts_execute:输入 / 触发快捷键
- computer:鼠标点击 / 拖拽 / 截图
- read_console_messages / read_network_requests:调试
- write_file:把结果保存到任务输出目录(相对路径)
- read_file:读项目根的输入文件(如 chase_messages_batch1.md)

工作流程建议:
1. tabs_context_mcp(createIfEmpty=true) 拿一个 tab
2. navigate 到目标 URL
3. javascript_tool / read_page 看页面状态
4. 按用户目标采取下一步行动
5. 任务完成后,如果有结构化结果,用 write_file 保存"""


def _required_outputs_ready(label: str) -> bool:
    """Return whether a skill has produced the artifacts required to finish.

    Most skills are open-ended, so only ecom-best-source has a hard guard here:
    it must write a final CSV directly in WORKER_OUTPUT_DIR. Scratch JSON under
    .ecom-scratch is intentionally not enough.
    """
    if label != "ecom-best-source":
        return True
    raw = os.environ.get("WORKER_OUTPUT_DIR", "").strip()
    if not raw:
        return True
    output_dir = Path(raw)
    try:
        return any(
            p.is_file()
            and p.suffix.lower() == ".csv"
            and not p.name.startswith(".")
            for p in output_dir.iterdir()
        )
    except OSError:
        return False


def _should_stop_ecom_after_csv(label: str, tool_calls: list[ToolCall]) -> bool:
    """Stop ecom-best-source once its final CSV exists.

    After the deterministic sourcing pipeline writes the CSV, extra tool calls
    are usually redundant verification: reading rules, reading the CSV, or
    rerunning scoring scripts. The master uploads artifacts from the output dir,
    so the worker can finish without letting those calls burn another minute or
    overwrite the already-good CSV.
    """
    if label != "ecom-best-source" or not tool_calls:
        return False
    return _required_outputs_ready(label)


async def _describe_image_blocks(
    content_list: list, multimodal_llm: LLMClient, tool_name: str,
) -> list:
    """Replace MCP image content blocks with multimodal-generated text
    descriptions. Without this the reasoning LLM (text-only) sees the
    image data as garbage in a JSON string and acts blindly — clicking
    random coordinates, retrying the same selector — until iteration cap
    kills the task.

    The multimodal model only sees the image to describe it; the description
    text is what the reasoning model receives. Failures degrade gracefully
    to a "[screenshot description failed: ...]" placeholder so the loop
    continues."""
    out: list = []
    for block in content_list or []:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if block.get("type") != "image":
            out.append(block)
            continue
        data = block.get("data", "")
        mime = block.get("mimeType", "image/png")
        if not data:
            out.append({"type": "text", "text": "[screenshot empty]"})
            continue
        try:
            desc = await multimodal_llm.describe_image(data, mime)
        except Exception as exc:
            log.warning("multimodal describe failed for %s: %s", tool_name, exc)
            out.append({"type": "text", "text": f"[screenshot description failed: {exc}]"})
            continue
        log.info("multimodal describe (%s): %d chars", tool_name, len(desc))
        out.append({
            "type": "text",
            "text": f"[Screenshot description (from multimodal model)]:\n{desc}",
        })
    return out


async def _run_chat_loop(
    config: WorkerConfig,
    system_prompt: str,
    label: str,
    user_task: str,
    openai_tools: list[dict],
    *,
    mcp: OpenClaudeInChromeClient | None,
    multimodal_llm: LLMClient,
    llm: LLMClient,
) -> int:
    knowledge_topics = _list_knowledge_topics()
    knowledge_hint = ""
    if knowledge_topics:
        knowledge_hint = (
            "\n\n## 可用 knowledge topics(用 read_knowledge 工具查阅)\n"
            + "\n".join(f"- {t}" for t in knowledge_topics)
        )
    # Prepend the operator guidance to every skill's system prompt so
    # the LLM knows it has full autonomy (screenshots, mouse clicks,
    # popup dismissal) without it being duplicated in every SKILL.md.
    # Freeform mode's system_prompt already includes the guidance via
    # FREEFORM_SYSTEM_PROMPT — skip the prepend to avoid duplication.
    full_system = (
        system_prompt
        if system_prompt.startswith(_WORKER_OPERATOR_GUIDANCE[:50])
        else _WORKER_OPERATOR_GUIDANCE + "\n\n---\n\n" + system_prompt
    )
    # Initial user message: include `--task` content if provided so the
    # skill body can act on user-supplied parameters (e.g. a JD URL the
    # user @-mentioned). Without --task, the skill body is the sole
    # instruction; with --task, the LLM's first turn sees both.
    user_first = f"执行任务: {label}"
    if user_task:
        user_first += f"\n\n用户消息原文:\n{user_task}"
    messages = [
        {"role": "system", "content": full_system + knowledge_hint},
        {"role": "user", "content": user_first},
    ]

    # Detect "extension not connected" — open-claude-in-chrome's MCP server
    # returns this as a SUCCESSFUL tool result (is_error=False) with the error
    # in content text.
    extension_disconnected = False
    productive_calls = 0
    missing_required_output_retries = 0

    for iteration in range(MAX_ITERATIONS):
        try:
            response = await llm.chat(messages, tools=openai_tools or None)
        except Exception as exc:
            log.error("LLM error: %s", exc)
            return 3

        if response.tool_calls:
            if _should_stop_ecom_after_csv(label, response.tool_calls):
                tool_names = ", ".join(tc.name for tc in response.tool_calls)
                log.info(
                    "worker=%s label=%s final CSV exists; stopping before post-completion tool calls: %s",
                    config.worker_id, label, tool_names,
                )
                return 0

            assistant_msg: dict = {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in response.tool_calls
                ],
            }
            if response.reasoning_content:
                assistant_msg["reasoning_content"] = response.reasoning_content
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                log.info("tool_call: %s args=%s", tc.name, tc.arguments[:200])

                if is_builtin(tc.name):
                    machine_id = _resolve_machine_id()
                    proj_root_env = os.environ.get("WORKER_PROJECT_ROOT", "").strip()
                    proj_root = Path(proj_root_env) if proj_root_env else Path.cwd()
                    result_text = execute_builtin(
                        tc.name, tc.arguments, proj_root,
                        machine_id=machine_id,
                        knowledge_root=_resolve_knowledge_root(),
                    )
                    is_error = False
                elif mcp is None:
                    result_text = (
                        f"error: tool {tc.name!r} is unavailable because this skill "
                        "does not enable browser MCP"
                    )
                    is_error = True
                else:
                    try:
                        args = json.loads(tc.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        result = await mcp.call_tool(tc.name, args)
                    except Exception as exc:
                        log.error("MCP tool %r failed: %s", tc.name, exc)
                        result_text = f"error: {exc}"
                        is_error = True
                    else:
                        content = result.content or []
                        has_image = any(
                            isinstance(b, dict) and b.get("type") == "image"
                            for b in content
                        )
                        if has_image:
                            content = await _describe_image_blocks(
                                content, multimodal_llm, tc.name,
                            )
                        result_text = json.dumps(content, ensure_ascii=False)
                        is_error = result.is_error

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

                if is_error:
                    log.warning("tool %r returned error: %s", tc.name, result_text[:200])
                else:
                    productive_calls += 1

                if "extension is not connected" in result_text.lower() or \
                   "extension not connected" in result_text.lower():
                    extension_disconnected = True

        elif response.finish_reason == "stop":
            if extension_disconnected:
                log.error(
                    "worker=%s label=%s LLM bailed (extension not connected) — exiting mcp-failed",
                    config.worker_id, label,
                )
                return 2
            if not _required_outputs_ready(label):
                missing_required_output_retries += 1
                if missing_required_output_retries > _ECOM_REQUIRED_OUTPUT_RETRIES:
                    log.error(
                        "worker=%s label=%s stopped without required CSV output — exiting skill-failed",
                        config.worker_id, label,
                    )
                    return 1
                log.warning(
                    "worker=%s label=%s stopped before required CSV output; asking LLM to continue",
                    config.worker_id, label,
                )
                messages.append({
                    "role": "assistant",
                    "content": response.text or "",
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "任务还没有完成。ecom-best-source 必须在任务输出目录直接写出 "
                        "1 个最终 CSV 文件，文件名形如 `找货_<商品简写>_<YYYYMMDD>.csv`。"
                        "中间 JSON / scratch 文件不算最终产出。请继续执行召回、筛选，优先用 "
                        "run_ecom_script 调用 `sourcing_pipeline.py --jd-product ... --candidates ... "
                        "--output ...csv` 生成最终 CSV；脚本成功后直接用 stdout 的 top3 总结，不要再重跑规则。"
                    ),
                })
                continue
            log.info("LLM finished. worker=%s label=%s", config.worker_id, label)
            return 0
        else:
            plain_msg: dict = {"role": "assistant", "content": response.text}
            if response.reasoning_content:
                plain_msg["reasoning_content"] = response.reasoning_content
            messages.append(plain_msg)

        if response.finish_reason == "stop" and not response.tool_calls:
            if extension_disconnected:
                log.error(
                    "worker=%s label=%s LLM bailed (extension not connected) — exiting mcp-failed",
                    config.worker_id, label,
                )
                return 2
            if productive_calls == 0:
                log.error(
                    "worker=%s label=%s LLM stopped with 0 productive tool calls — exiting skill-failed",
                    config.worker_id, label,
                )
                return 1
            return 0

    log.error("hit max iterations (%d) without finish", MAX_ITERATIONS)
    return 1


async def run(
    config: WorkerConfig,
    system_prompt: str,
    label: str,
    user_task: str = "",
    *,
    requires_browser_mcp: bool = True,
) -> int:
    reasoning = config.llm_reasoning
    llm = LLMClient(
        base_url=reasoning.base_url,
        api_key=reasoning.api_key,
        model=reasoning.model,
    )
    multimodal_cfg = config.llm_multimodal
    multimodal_llm = LLMClient(
        base_url=multimodal_cfg.base_url,
        api_key=multimodal_cfg.api_key,
        model=multimodal_cfg.model,
    )

    if not requires_browser_mcp:
        log.info(
            "worker=%s label=%s browser_mcp=disabled builtin_tools=%d",
            config.worker_id, label, len(BUILTIN_TOOLS),
        )
        return await _run_chat_loop(
            config, system_prompt, label, user_task, BUILTIN_TOOLS,
            mcp=None, multimodal_llm=multimodal_llm, llm=llm,
        )

    try:
        async with OpenClaudeInChromeClient(
            port=config.mcp_port,
            mcp_server_js_path=config.mcp_server_js_path,
        ) as mcp:
            tools = await mcp.list_tools()
            openai_tools = tools_to_openai_functions(tools) + BUILTIN_TOOLS
            log.info(
                "worker=%s label=%s mcp_tools=%d builtin_tools=%d",
                config.worker_id, label, len(tools), len(BUILTIN_TOOLS),
            )
            return await _run_chat_loop(
                config, system_prompt, label, user_task, openai_tools,
                mcp=mcp, multimodal_llm=multimodal_llm, llm=llm,
            )
    except OSError as exc:
        log.error("MCP connect failed port=%d: %s", config.mcp_port, exc)
        return 2


def _resolve_knowledge_root() -> Path:
    """Best-effort: read knowledge.root from config.yaml, fallback to ./knowledge.
    Config now mandates absolute paths; this just reads what's there."""
    try:
        from agent.config import load_config_file
        cfg = load_config_file()
        k = cfg.get("knowledge")
        if isinstance(k, dict):
            root = str(k.get("root", "")).strip()
            if root:
                p = Path(os.path.expanduser(root))
                if p.is_absolute():
                    return p
    except Exception:
        pass
    return Path("./knowledge")


def _list_knowledge_topics() -> list[str]:
    """Best-effort: list curated/+by-machine knowledge topics for system prompt."""
    try:
        from agent.knowledge_store import KnowledgeStore
        return KnowledgeStore(root=_resolve_knowledge_root()).list_topics()
    except Exception:
        return []


def _resolve_machine_id() -> str:
    """Read top-level machine_name from config.yaml. Falls back to 'unknown'.
    Used to tag knowledge entries with which machine produced them — silently
    falling through to 'unknown' would silently break the merger's by-machine
    aggregation."""
    try:
        from agent.config import load_config_file
        cfg = load_config_file()
        name = cfg.get("machine_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return "unknown"


def _load_skill_body(config: WorkerConfig, skill_name: str) -> tuple[str, str, bool] | None:
    skills_dir = config.skills_dir
    if not skills_dir.is_absolute():
        skills_dir = Path.cwd() / skills_dir
    registry = SkillRegistry(skills_dir)
    try:
        skill = registry.load_full(skill_name)
    except KeyError:
        log.error("skill %r not found in %s", skill_name, skills_dir)
        return None
    return (skill.body, skill.name, skill.requires_browser_mcp)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m agent.worker",
        description="1688 invoice automation worker agent",
    )
    parser.add_argument("--worker-id", required=True, help="worker identifier, e.g. b1")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--skill", help="skill name from skills/ dir, e.g. fapiao-1688")
    mode.add_argument("--freeform", help="ad-hoc natural-language task; runs without a SKILL.md")
    parser.add_argument(
        "--task",
        default="",
        help=(
            "Optional runtime input forwarded to the skill as the first user "
            "message. Used when a skill needs a parameter the SKILL.md can't "
            "hardcode — e.g. ecom-best-source needs the JD product URL the "
            "user mentioned in chat. Only meaningful with --skill; ignored in "
            "--freeform mode (freeform task text already lives in the system "
            "prompt)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="MCP server TCP port (default: 18764 + worker index)",
    )
    args = parser.parse_args()

    config = worker_config_from_file(args.worker_id, mcp_port=args.port)

    if args.skill:
        loaded = _load_skill_body(config, args.skill)
        if loaded is None:
            sys.exit(4)
        system_prompt, label, requires_browser_mcp = loaded
    else:
        system_prompt = FREEFORM_SYSTEM_PROMPT + "\n\n用户任务:\n" + args.freeform
        label = f"freeform({args.freeform[:40]}{'...' if len(args.freeform) > 40 else ''})"
        requires_browser_mcp = True

    user_task = args.task if args.skill else ""
    exit_code = asyncio.run(run(
        config,
        system_prompt,
        label,
        user_task=user_task,
        requires_browser_mcp=requires_browser_mcp,
    ))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
