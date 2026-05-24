from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import BotConfig, LLMSettings
from agent.llm_client import ChatResponse
from agent.worker_state import WorkerStateTracker


_BOT_CFG = BotConfig(
    enabled=True,
    app_id="cli_test",
    app_secret="secret_test",
    authorized_user_ids=("ou_authorized",),
)
_MACHINE = "pc-test"

_LLM_SETTINGS = LLMSettings(
    base_url="http://localhost:9999/v1",
    model="test-model",
    api_key="test-key",
)


def _make_fake_msg(text: str, open_id: str = "ou_authorized", chat_id: str = "oc_chat1") -> MagicMock:
    msg = MagicMock()
    msg.content_text = text
    msg.chat_id = chat_id
    sender = MagicMock()
    sender.open_id = open_id
    msg.sender = sender
    return msg


def _make_dispatcher(tmp_path: Path) -> MagicMock:
    from agent.intents import MasterDispatcher
    d = MagicMock(spec=MasterDispatcher)
    d.worker_state = WorkerStateTracker()
    d.log_dir = tmp_path
    d.restart_worker = AsyncMock()
    d.set_paused = AsyncMock()
    return d


@pytest.mark.asyncio
async def test_bot_authorized_message_triggers_reply(tmp_path: Path):
    dispatcher = _make_dispatcher(tmp_path)
    captured_replies: list[str] = []

    mock_channel = MagicMock()
    mock_channel.on = MagicMock()
    mock_channel.connect_until_ready = AsyncMock()
    mock_channel.stop_background = AsyncMock()
    mock_channel.send = AsyncMock(side_effect=lambda chat_id, msg: captured_replies.append(msg))

    registered_handler = None

    def capture_on(event_name, handler):
        nonlocal registered_handler
        if event_name == "message":
            registered_handler = handler

    mock_channel.on.side_effect = capture_on

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient") as MockLLM:
        mock_llm_instance = MagicMock()
        mock_llm_instance.chat = AsyncMock(
            return_value=ChatResponse(
                text=json.dumps({"intent": "query_status", "args": {}}),
                tool_calls=[],
                finish_reason="stop",
            )
        )
        MockLLM.return_value = mock_llm_instance

        from agent.bot import run_bot

        mock_channel.connect_until_ready.side_effect = AsyncMock(return_value=None)

        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            assert registered_handler is not None
            fake_msg = _make_fake_msg("查状态")
            await registered_handler(fake_msg)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    assert len(captured_replies) == 1
    sent = captured_replies[0]
    assert "card" in sent
    title = sent["card"]["header"]["title"]["content"]
    assert "pc-test" in title


@pytest.mark.asyncio
async def test_bot_unauthorized_message_ignored(tmp_path: Path):
    dispatcher = _make_dispatcher(tmp_path)
    captured_replies: list[str] = []

    mock_channel = MagicMock()
    mock_channel.on = MagicMock()
    mock_channel.connect_until_ready = AsyncMock()
    mock_channel.stop_background = AsyncMock()
    mock_channel.send = AsyncMock(side_effect=lambda chat_id, msg: captured_replies.append(msg))

    registered_handler = None

    def capture_on(event_name, handler):
        nonlocal registered_handler
        if event_name == "message":
            registered_handler = handler

    mock_channel.on.side_effect = capture_on

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient"):

        from agent.bot import run_bot
        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            assert registered_handler is not None
            fake_msg = _make_fake_msg("查状态", open_id="ou_stranger")
            await registered_handler(fake_msg)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    assert len(captured_replies) == 0


@pytest.mark.asyncio
async def test_bot_disabled_does_not_connect(tmp_path: Path):
    """A disabled bot must not be in active_bots() — so main_loop skips it."""
    from agent.config import MasterConfig, WorkerConfig

    _DUMMY_LLM = LLMSettings(base_url="http://localhost:9999/v1", model="test", api_key="test")
    disabled_cfg = BotConfig(
        enabled=False,
        app_id="cli_test",
        app_secret="secret",
        authorized_user_ids=(),
    )
    workers = [
        WorkerConfig(
            worker_id=f"b{i}",
            mcp_port=18764 + i,
            llm_multimodal=_DUMMY_LLM,
            llm_reasoning=_DUMMY_LLM,
            log_dir=tmp_path,
        )
        for i in range(1, 7)
    ]
    config = MasterConfig(workers=workers, log_dir=tmp_path, bots=(disabled_cfg,))
    assert config.active_bots() == []


@pytest.mark.asyncio
async def test_active_bots_filters_disabled(tmp_path: Path):
    """active_bots() returns only enabled entries from the `bots:` list."""
    from agent.config import MasterConfig

    bot1 = BotConfig(enabled=True, app_id="b1", app_secret="x", authorized_user_ids=())
    bot2 = BotConfig(enabled=False, app_id="b2", app_secret="x", authorized_user_ids=())
    cfg = MasterConfig(workers=[], bots=(bot1, bot2))
    active = cfg.active_bots()
    assert [b.app_id for b in active] == ["b1"]


@pytest.mark.asyncio
async def test_bot_empty_text_not_processed(tmp_path: Path):
    dispatcher = _make_dispatcher(tmp_path)
    captured_replies: list[str] = []

    mock_channel = MagicMock()
    mock_channel.on = MagicMock()
    mock_channel.connect_until_ready = AsyncMock()
    mock_channel.stop_background = AsyncMock()
    mock_channel.send = AsyncMock(side_effect=lambda chat_id, msg: captured_replies.append(msg))

    registered_handler = None

    def capture_on(event_name, handler):
        nonlocal registered_handler
        if event_name == "message":
            registered_handler = handler

    mock_channel.on.side_effect = capture_on

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient"):

        from agent.bot import run_bot
        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            fake_msg = _make_fake_msg("   ")
            await registered_handler(fake_msg)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    assert len(captured_replies) == 0


# ── Task #4: sanitize input ──────────────────────────────────────────────────

def test_bot_sanitize_strips_at_markup():
    from agent.bot import _sanitize
    raw = '<at user_id="ou_abc">@AI Chrome Assistant</at> 查状态'
    assert _sanitize(raw) == "查状态"


def test_bot_sanitize_strips_zero_width():
    from agent.bot import _sanitize
    assert _sanitize("​查状态​") == "查状态"


def test_bot_sanitize_strips_whitespace():
    from agent.bot import _sanitize
    assert _sanitize("  查状态  ") == "查状态"


def test_bot_sanitize_combined():
    from agent.bot import _sanitize
    raw = '<at user_id="ou_abc">@Bot</at>  ​查状态​  '
    assert _sanitize(raw) == "查状态"


@pytest.mark.asyncio
async def test_bot_sanitize_input_before_nlu(tmp_path: Path):
    dispatcher = _make_dispatcher(tmp_path)
    captured_nlu_texts: list[str] = []

    mock_channel = MagicMock()
    registered: dict = {}

    def capture_on(event_name, handler):
        registered[event_name] = handler

    mock_channel.on.side_effect = capture_on
    mock_channel.send = AsyncMock()

    async def fake_route(text, llm, skills_dir=None, recent_turns=None):
        captured_nlu_texts.append(text)
        from agent.nlu import Intent, IntentDispatch
        return IntentDispatch(intent=Intent.QUERY_STATUS, args={})

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient"), \
         patch("agent.bot.route", side_effect=fake_route):

        from agent.bot import run_bot
        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            fake_msg = _make_fake_msg('<at user_id="ou_abc">@Bot</at> 查状态')
            await registered["message"](fake_msg)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    assert len(captured_nlu_texts) == 1
    assert "<at" not in captured_nlu_texts[0]
    assert "查状态" in captured_nlu_texts[0]


# ── Task #6: card action dispatch ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bot_card_action_dispatch(tmp_path: Path):
    """Card-button path with the REAL lark_oapi CardActionEvent dataclass —
    not a dict — because that's what the SDK actually passes in production."""
    from lark_oapi.channel.types import CardActionEvent, CardActionPayload, EventOperator

    dispatcher = _make_dispatcher(tmp_path)
    dispatcher.restart_worker = AsyncMock()
    captured_replies: list[dict] = []

    mock_channel = MagicMock()
    registered: dict = {}

    def capture_on(event_name, handler):
        registered[event_name] = handler

    mock_channel.on.side_effect = capture_on
    mock_channel.send = AsyncMock(side_effect=lambda chat_id, msg: captured_replies.append(msg))

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient"):

        from agent.bot import run_bot
        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            assert "cardAction" in registered
            event = CardActionEvent(
                message_id="m1",
                chat_id="oc_chat1",
                operator=EventOperator(open_id="ou_authorized"),
                action=CardActionPayload(
                    value={"intent": "restart_worker", "args": {"worker_id": "b3"}},
                ),
            )
            await registered["cardAction"](event)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    # bot.py now builds a ReplyTarget per message and threads it through —
    # so dispatcher.restart_worker is called with (worker_id, reply_to).
    dispatcher.restart_worker.assert_called_once()
    args, _ = dispatcher.restart_worker.call_args
    assert args[0] == "b3"
    from agent.channels import ReplyTarget
    assert isinstance(args[1], ReplyTarget)
    assert args[1].target_id == "oc_chat1"
    assert len(captured_replies) == 1


@pytest.mark.asyncio
async def test_bot_card_action_unknown_intent_ignored(tmp_path: Path):
    from lark_oapi.channel.types import CardActionEvent, CardActionPayload, EventOperator
    dispatcher = _make_dispatcher(tmp_path)
    captured_replies: list[dict] = []

    mock_channel = MagicMock()
    registered: dict = {}

    def capture_on(event_name, handler):
        registered[event_name] = handler

    mock_channel.on.side_effect = capture_on
    mock_channel.send = AsyncMock(side_effect=lambda chat_id, msg: captured_replies.append(msg))

    with patch("agent.bot.FeishuChannel", return_value=mock_channel), \
         patch("agent.bot.LLMClient"):

        from agent.bot import run_bot
        import asyncio

        async def run():
            task = asyncio.create_task(run_bot(_BOT_CFG, _LLM_SETTINGS, dispatcher, machine_name=_MACHINE))
            await asyncio.sleep(0)
            event = CardActionEvent(
                message_id="m1",
                chat_id="oc_chat1",
                operator=EventOperator(open_id="ou_authorized"),
                action=CardActionPayload(
                    value={"intent": "not_a_real_intent", "args": {}},
                ),
            )
            await registered["cardAction"](event)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run()

    assert len(captured_replies) == 0
