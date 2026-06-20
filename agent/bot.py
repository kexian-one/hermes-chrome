from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from lark_oapi.channel import FeishuChannel

from agent.channels import ReplyTarget
from agent.config import BotConfig, LLMSettings
from agent.conversation import ConversationBuffer
from agent.intents import MasterDispatcher, handle
from agent.llm_client import LLMClient
from agent.nlu import IntentDispatch, route

# How many user messages to pull from Feishu chat history per @bot trigger.
# Each NLU call adds one im.v1.message.list API hit (~200-400ms latency).
# Survives master restarts because the source-of-truth is Feishu, not memory.
_CHAT_HISTORY_LIMIT = 10
# Don't pull messages older than this — keep "context" recent, not full archive.
_CHAT_HISTORY_MAX_AGE_SECS = 30 * 60   # 30 minutes

if TYPE_CHECKING:
    from lark_oapi.channel.types import InboundMessage

logger = logging.getLogger(__name__)

_AT_TAG_RE = re.compile(r"<at[^>]*>.*?</at>", re.DOTALL)
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")

# File extensions Feishu should send as Image (preview inline) vs File (download).
# Anything not in this set gets sent as File regardless of suffix.
_FEISHU_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


async def _feishu_send_file(channel, target_id: str, file_path: str) -> None:
    """Upload a local file to Feishu and send it as a file/image message.

    Bound onto each FeishuChannel instance as `.send_file` (via setattr) so
    that the channel-agnostic `ReplyTarget.send_file` can find and call it.
    Master's `_notify_task_done` invokes this whenever a worker produces an
    artifact under the allowlist (csv/xlsx/pdf/png/...).

    Image extensions go out as Feishu Image messages (inline preview); other
    suffixes go as File messages (with the filename visible in chat).
    """
    from pathlib import Path as _Path
    from lark_oapi.channel.types import MediaSource
    from lark_oapi.channel._coerce import OutboundFile, OutboundImage

    path = _Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"send_file: {file_path} not found")

    source = MediaSource(kind="file", path=str(path))
    if path.suffix.lower() in _FEISHU_IMAGE_SUFFIXES:
        outbound = OutboundImage(source=source)
    else:
        outbound = OutboundFile(source=source, file_name=path.name)
    await channel.send(target_id, outbound)


def _sanitize(text: str) -> str:
    text = _AT_TAG_RE.sub("", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    return text.strip()


async def _fetch_parent_text(channel, parent_id: str) -> str:
    """Fetch the text of a message by ID. Used when the user "replies" to a
    specific prior message — gives NLU the parent's text as quote context.

    Robust against lark_oapi's `InboundMessage.reply` field not always being
    populated for reply events — we read parent_id straight from msg.raw
    and call this to get the actual content.

    Requires `im:message` (or `im:message:readonly`) permission.
    Returns "" on any failure (missing permission, deleted message, etc.).
    """
    if not parent_id:
        return ""
    import json as _json
    # Retry transient network/DNS issues (getaddrinfo failed, connection
    # reset, etc.) — Feishu REST API call can flap independently of the
    # WebSocket event channel. Short, fast retries; failing all is OK.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            data = await channel.fetch_message(parent_id)
            items = (data or {}).get("data", {}).get("items", [])
            if not items:
                return ""
            body = items[0].get("body", {})
            content_raw = body.get("content", "")
            try:
                content = _json.loads(content_raw) if content_raw else {}
            except (ValueError, TypeError):
                return ""
            text = content.get("text", "") if isinstance(content, dict) else ""
            return _sanitize(text) if isinstance(text, str) else ""
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))   # 0.5s, 1s
    logger.warning("fetch parent message %s failed after 3 attempts: %s", parent_id, last_exc)
    return ""


async def _fetch_chat_context(
    channel, chat_id: str, exclude_message_id: str, bot_open_id: str,
) -> list[tuple[str, str, str]]:
    """Pull the last N text/post messages from this Feishu chat as NLU context.

    Returns a list of `(user_text, intent_str, summary)` tuples matching
    `route()`'s `recent_turns` shape — so the same NLU prompt format works
    whether context comes from in-memory buffer or live Feishu history.

    - Skips bot's OWN previous messages (cards aren't useful for intent recovery).
    - Skips the current message being processed (exclude_message_id).
    - Skips messages older than _CHAT_HISTORY_MAX_AGE_SECS.
    - Requires `im:message.group_msg` permission on the Feishu app
      (官方文档:"获取群组中所有消息" — 飞书开放平台 → 权限管理 → 搜该权限名 →
      添加 → 重新发布版本生效).

    Failures (missing permission, network) return [] silently — caller falls
    back to in-memory buffer.
    """
    try:
        import json as _json
        from lark_oapi.api.im.v1 import ListMessageRequest

        req = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .sort_type("ByCreateTimeDesc")    # newest first
            .page_size(_CHAT_HISTORY_LIMIT + 5)   # +5 buffer for filtered-out bot msgs
            .build()
        )
        resp = await channel.client.im.v1.message.alist(req)
        if not (resp and getattr(resp, "data", None) and getattr(resp.data, "items", None)):
            return []

        cutoff_ms = int((datetime.now(tz=timezone.utc).timestamp() - _CHAT_HISTORY_MAX_AGE_SECS) * 1000)
        turns: list[tuple[str, str, str]] = []

        # API gives newest first; iterate and collect user messages, oldest→newest
        # at the end (NLU prompt expects chronological order).
        for m in resp.data.items:
            if m.message_id == exclude_message_id:
                continue
            create_ts = int(m.create_time or 0)
            if create_ts < cutoff_ms:
                break    # rest are even older
            sender = m.sender
            is_bot = (sender and (sender.sender_type == "app" or sender.id == bot_open_id))
            if is_bot:
                # Bot replies are cards — too structured to feed as user-style
                # NLU context. Could parse the card title later, but skipped now.
                continue
            if m.msg_type not in ("text", "post"):
                continue
            try:
                body = _json.loads(m.body.content) if m.body and m.body.content else {}
            except (ValueError, AttributeError):
                continue
            text = body.get("text", "") if isinstance(body, dict) else ""
            text = _sanitize(text)
            if not text:
                continue
            # We don't know the intent of past messages — leave it blank;
            # the NLU prompt already handles "user said X" without intent label.
            turns.append((text, "", ""))

        # API gave newest-first; reverse to chronological for the prompt.
        return list(reversed(turns[:_CHAT_HISTORY_LIMIT]))
    except Exception as exc:
        logger.warning("fetch chat context failed (missing im:message.group_msg?): %s", exc)
        return []


def _card_title(card: dict) -> str:
    try:
        return card["header"]["title"]["content"]
    except (KeyError, TypeError):
        return ""


async def run_bot(
    config: BotConfig, llm_settings: LLMSettings, master: MasterDispatcher,
    machine_name: str = "machine",
) -> None:
    channel = FeishuChannel(
        app_id=config.app_id,
        app_secret=config.app_secret,
    )

    # NLU is short intent classification — disable thinking explicitly.
    # DeepSeek's thinking is on by default and adds 5-30s of reasoning per call,
    # which a) makes "查状态" feel sluggish and b) raises stream-truncation
    # rate. Worker LLMClient (in agent/worker.py) keeps default behavior so
    # complex skills can still benefit from thinking.
    # `extra_body` is forwarded as-is; non-DeepSeek providers will ignore the
    # unknown field.
    llm = LLMClient(
        base_url=llm_settings.base_url,
        api_key=llm_settings.api_key,
        model=llm_settings.model,
        provider=llm_settings.provider,
        extra_body={"thinking": {"type": "disabled"}},
        max_tokens=2048,
        temperature=0.0,
        mcp_server_config=llm_settings.mcp_server_config,
        mcp_tool=llm_settings.mcp_tool,
    )

    buffer = ConversationBuffer(max_per_chat=5)

    async def on_message(msg: InboundMessage) -> None:
        sender_open_id = msg.sender.open_id if msg.sender else ""
        if config.authorized_user_ids and sender_open_id not in config.authorized_user_ids:
            logger.warning("unauthorized message from %s, ignoring", sender_open_id)
            return

        raw_text = msg.content_text or ""
        text = _sanitize(raw_text)
        if not text:
            return

        chat_id = msg.chat_id or ""

        # Pull recent chat history from Feishu (source of truth, survives
        # master restarts). Falls back to in-memory buffer on failure (e.g.
        # missing `im:message:readonly` permission, network blip).
        bot_open_id = ""
        try:
            ident = getattr(channel, "bot_identity", None)
            if ident is not None:
                bot_open_id = getattr(ident, "open_id", "") or ""
        except Exception:
            pass
        recent = await _fetch_chat_context(channel, chat_id, msg.id, bot_open_id)
        fetched_from_feishu = bool(recent)
        if not recent:
            recent = buffer.recent(chat_id)

        # Feishu "reply / 引用": user pointed at one specific message.
        # Read parent_id from raw payload (more reliable than the
        # InboundMessage.reply field which is sometimes None) and pull the
        # parent's text via fetch_message. This is the strongest context
        # signal — user is explicitly saying "重做 / 改成 / 重试 this one".
        # Note: msg.raw IS the message dict at top level — parent_id is
        # directly on it, NOT under a .message subkey.
        raw_dict = msg.raw if isinstance(msg.raw, dict) else {}
        parent_id = raw_dict.get("parent_id") or raw_dict.get("root_id") or ""
        # InboundMessage.reply.text (if populated) is a free shortcut, but
        # don't trust isinstance check since MagicMock in tests is truthy.
        reply_obj = getattr(msg, "reply", None)
        reply_text_attr = getattr(reply_obj, "text", None) if reply_obj is not None else None
        quoted = ""
        if isinstance(reply_text_attr, str) and reply_text_attr:
            quoted = _sanitize(reply_text_attr)
        elif parent_id:
            quoted = await _fetch_parent_text(channel, parent_id)
        if quoted and quoted != text:
            text = f"[用户引用了上一条消息]: {quoted}\n[用户本次说]: {text}"
        # For diagnostic visibility (overrides earlier reply_text used in print)
        reply_text = quoted or None

        # Diagnostic — remove after stabilizing. Helps see what NLU actually got.
        raw_keys = list(raw_dict.keys())
        print(
            f"[bot] msg id={msg.id} text={text[:100]!r} "
            f"parent_id={parent_id!r} quoted={quoted[:60]!r} "
            f"raw.keys={raw_keys} "
            f"history_src={'feishu' if fetched_from_feishu else 'memory'} "
            f"history_n={len(recent)}",
            flush=True,
        )

        # Origin ReplyTarget — completion notifications route back to THIS chat,
        # on THIS bot. supports_files=True now that _feishu_send_file is
        # attached to the channel (above).
        reply_to = ReplyTarget(
            channel=channel, target_id=chat_id,
            supports_files=True, machine_name=machine_name,
        )

        try:
            dispatch = await route(
                text, llm,
                skills_dir=getattr(master, "skills_dir", None),
                recent_turns=recent if recent else None,
            )
            print(f"[bot] NLU → intent={dispatch.intent} args={dispatch.args}", flush=True)
            # If NLU couldn't make sense of it AND we had context-failure
            # signals (parent quoted but unfetchable, no chat history),
            # explain WHY — "没听懂" is misleading when the real cause is
            # transient network error fetching the quoted message.
            from agent.nlu import Intent as _Intent
            if (
                dispatch.intent == _Intent.UNKNOWN
                and parent_id and not quoted
                and not recent
            ):
                from agent.cards import warning_card
                reply_card = warning_card(
                    f"[{machine_name}] 上下文取不到",
                    f"你引用的消息我拿不到原文(可能临时网络抖动)。\n"
                    f"请直接发完整指令(例如 `@bot b2 去催发票`),不要用引用回复。",
                )
            else:
                reply_card = await handle(dispatch, master, machine_name, reply_to)
        except Exception as exc:
            logger.exception("error handling message: %s", exc)
            from agent.cards import error_card
            reply_card = error_card(f"[{machine_name}] 处理出错", str(exc))
            dispatch = IntentDispatch(intent=None, args={})  # type: ignore[arg-type]

        summary = _card_title(reply_card)
        intent_str = dispatch.intent.value if dispatch.intent else "unknown"
        # Buffer stores the user-visible text (no synthetic prefix) — used as
        # fallback when Feishu history fetch fails.
        original_text = (
            text.split("[用户本次说]: ", 1)[-1]
            if "[用户本次说]: " in text else text
        )
        buffer.append(chat_id, original_text, intent_str, summary)

        try:
            await channel.send(msg.chat_id, {"card": reply_card})
        except Exception as exc:
            logger.exception("error sending reply: %s", exc)

    async def on_card_action(event) -> None:
        # lark_oapi passes a CardActionEvent dataclass (has .chat_id / .operator /
        # .action), NOT a dict. Tests sometimes hand-build dicts — accept both.
        if isinstance(event, dict):
            action = event.get("action", {})
            value = (action or {}).get("value", {}) if isinstance(action, dict) else {}
            chat_id = event.get("open_chat_id", "") or event.get("chat_id", "")
            sender_open_id = (
                event.get("user_id")
                or event.get("open_id")
                or (event.get("operator", {}) or {}).get("open_id", "")
                or ""
            )
        else:
            chat_id = getattr(event, "chat_id", "") or ""
            operator = getattr(event, "operator", None)
            sender_open_id = getattr(operator, "open_id", "") if operator else ""
            action = getattr(event, "action", None)
            value = getattr(action, "value", None) if action else None
            if not isinstance(value, dict):
                value = {}

        # Same auth check as on_message: if whitelist is set, sender must be in it.
        if config.authorized_user_ids and sender_open_id not in config.authorized_user_ids:
            logger.warning("unauthorized card action from %s, ignoring", sender_open_id)
            return

        intent_str = value.get("intent", "")
        args = value.get("args", {})

        if not intent_str:
            logger.warning("card action missing intent in value: %s", value)
            return

        from agent.nlu import Intent, IntentDispatch
        try:
            intent = Intent(intent_str)
        except ValueError:
            logger.warning("card action unknown intent: %s", intent_str)
            return

        reply_to = ReplyTarget(
            channel=channel, target_id=chat_id,
            supports_files=True, machine_name=machine_name,
        )
        try:
            reply_card = await handle(
                IntentDispatch(intent=intent, args=args), master, machine_name, reply_to,
            )
        except Exception as exc:
            logger.exception("error handling card action: %s", exc)
            from agent.cards import error_card
            reply_card = error_card(f"[{machine_name}] 按钮操作出错", str(exc))

        try:
            await channel.send(chat_id, {"card": reply_card})
        except Exception as exc:
            logger.exception("error sending card action reply: %s", exc)

    # Attach send_file onto the channel instance so the channel-agnostic
    # ReplyTarget.send_file (in agent/channels.py) can find and call it.
    # MUST be set BEFORE master.register_channel below — otherwise
    # default_reply_target's stored supports_files=True would point at a
    # channel that doesn't actually have send_file.
    async def _channel_send_file(target_id, file_path):
        return await _feishu_send_file(channel, target_id, file_path)
    channel.send_file = _channel_send_file

    channel.on("message", on_message)
    # ONLY register cardAction — earlier diagnosis showed registering
    # cardAction + card_action + interaction caused the SAME card-click to
    # fire the handler 3 times (they alias to the same underlying event in
    # lark_oapi). Single registration = single dispatch.
    channel.on("cardAction", on_card_action)
    logger.info("Bot starting in thread (machine=%s app_id=%s)", machine_name, config.app_id)

    # Multi-stage startup signal:
    #   - `setup_ok`: thread has done its synchronous setup (import / loop patch / on()) and is
    #     about to enter channel.start(). If this never fires, something exploded BEFORE start.
    #   - `start_failed`: channel.start() raised synchronously (most common: bad app_id/secret
    #     or no network). Means the connection definitely doesn't work.
    #   - The "no failure within probe window" path: setup_ok fired, no exception within ~3s →
    #     we assume the connection is live. This isn't bulletproof (a slow hang would still
    #     register), but channel.send() failures are caught and logged, so a dead channel
    #     degrades to "warnings in log + missed cards" rather than crashing master.
    setup_ok = threading.Event()
    start_failed = threading.Event()
    _START_PROBE_SECS = 3.0

    def _bot_thread_main():
        try:
            import lark_oapi.ws.client as ws_client
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            old_loop_id = id(ws_client.loop)
            ws_client.loop = new_loop
            print(
                f"[bot thread] patched ws_client.loop: old_id={old_loop_id} "
                f"new_id={id(new_loop)} running={ws_client.loop.is_running()}",
                flush=True,
            )
            setup_ok.set()
            channel.start()    # blocks until connection drops
        except Exception as exc:
            print(f"[bot thread] failed: {type(exc).__name__}: {exc}", flush=True)
            import traceback
            traceback.print_exc()
            start_failed.set()

    bot_thread = threading.Thread(
        target=_bot_thread_main,
        name=f"feishu-bot-{config.app_id}",
        daemon=True,
    )
    bot_thread.start()

    # Wait for setup_ok (or fail) — use a timeout so a wedged thread doesn't
    # leave run_bot waiting forever.
    waited = 0.0
    while not (setup_ok.is_set() or start_failed.is_set()):
        if waited >= 10.0:
            logger.error(
                "Bot app_id=%s: setup didn't complete within 10s — assuming dead, not registering",
                config.app_id,
            )
            return
        await asyncio.sleep(0.05)
        waited += 0.05

    # Probe window: give channel.start() a few seconds to flip start_failed.
    # If it doesn't, treat as healthy and register.
    probe_elapsed = 0.0
    while probe_elapsed < _START_PROBE_SECS:
        if start_failed.is_set():
            break
        await asyncio.sleep(0.1)
        probe_elapsed += 0.1

    if start_failed.is_set():
        logger.error("Bot app_id=%s failed to start — not registering with master", config.app_id)
        return

    # Register with the master only after the probe window passes cleanly.
    if hasattr(master, "register_channel"):
        try:
            master.register_channel(
                channel, config.alert_chat_id,
                supports_files=True,
                machine_name=machine_name,
                is_alert_target=config.is_alert_target,
            )
        except Exception as exc:
            logger.warning("master.register_channel failed: %s", exc)

    try:
        await asyncio.Event().wait()
    finally:
        try:
            channel.stop()
        except Exception:
            pass
