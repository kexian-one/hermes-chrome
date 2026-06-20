from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.llm_client import ChatResponse, LLMClient
from agent.nlu import Intent, IntentDispatch, route


def _mock_llm(response_json: dict) -> LLMClient:
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(
        return_value=ChatResponse(
            text=json.dumps(response_json),
            tool_calls=[],
            finish_reason="stop",
        )
    )
    return llm


@pytest.mark.asyncio
async def test_route_query_status():
    llm = _mock_llm({"intent": "query_status", "args": {}})
    result = await route("查状态", llm)
    assert result.intent == Intent.QUERY_STATUS
    assert result.args == {}


@pytest.mark.asyncio
async def test_route_restart_worker():
    llm = _mock_llm({"intent": "restart_worker", "args": {"worker_id": "b3"}})
    result = await route("重启 b3", llm)
    assert result.intent == Intent.RESTART_WORKER
    assert result.args == {"worker_id": "b3"}


@pytest.mark.asyncio
async def test_route_query_stats():
    llm = _mock_llm({"intent": "query_stats", "args": {}})
    result = await route("今天的统计", llm)
    assert result.intent == Intent.QUERY_STATS


@pytest.mark.asyncio
async def test_route_unknown_garbage():
    llm = _mock_llm({"intent": "unknown", "args": {}})
    result = await route("asdfqwerty随机垃圾", llm)
    assert result.intent == Intent.UNKNOWN


@pytest.mark.asyncio
async def test_route_malformed_json_falls_back_to_unknown():
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(
        return_value=ChatResponse(
            text="this is not json at all",
            tool_calls=[],
            finish_reason="stop",
        )
    )
    result = await route("some text", llm)
    assert result.intent == Intent.UNKNOWN


@pytest.mark.asyncio
async def test_route_llm_exception_falls_back_to_unknown():
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(side_effect=RuntimeError("network error"))
    result = await route("查状态", llm)
    assert result.intent == Intent.UNKNOWN


@pytest.mark.asyncio
async def test_route_invalid_intent_string_falls_back_to_unknown():
    llm = _mock_llm({"intent": "not_a_real_intent", "args": {}})
    result = await route("some text", llm)
    assert result.intent == Intent.UNKNOWN


@pytest.mark.asyncio
async def test_route_query_logs():
    llm = _mock_llm({"intent": "query_logs", "args": {"worker_id": "b2"}})
    result = await route("看 b2 日志", llm)
    assert result.intent == Intent.QUERY_LOGS
    assert result.args["worker_id"] == "b2"


@pytest.mark.asyncio
async def test_route_help():
    llm = _mock_llm({"intent": "help", "args": {}})
    result = await route("/help", llm)
    assert result.intent == Intent.HELP


@pytest.mark.asyncio
async def test_route_pause_all():
    llm = _mock_llm({"intent": "pause_all", "args": {}})
    result = await route("暂停所有", llm)
    assert result.intent == Intent.PAUSE_ALL


@pytest.mark.asyncio
async def test_route_resume_all():
    llm = _mock_llm({"intent": "resume_all", "args": {}})
    result = await route("继续", llm)
    assert result.intent == Intent.RESUME_ALL


@pytest.mark.asyncio
async def test_route_run_now():
    llm = _mock_llm({"intent": "run_now", "args": {"worker_id": "b2", "skill": "fapiao-1688"}})
    result = await route("b2 现在跑 fapiao-1688", llm)
    assert result.intent == Intent.RUN_NOW
    assert result.args == {"worker_id": "b2", "skill": "fapiao-1688"}


@pytest.mark.asyncio
async def test_route_ecom_preserves_jd_url_when_llm_summarizes_task():
    llm = _mock_llm({
        "intent": "run_now",
        "args": {"worker_id": "b1", "skill": "ecom-best-source", "task": "要3"},
    })
    result = await route(
        "@开票助手1号 [六神花露水 - 京东](https://item.jd.com/10159624742014.html) 要3",
        llm,
    )
    assert result.intent == Intent.RUN_NOW
    assert result.args["task"] == "要3 https://item.jd.com/10159624742014.html"


@pytest.mark.asyncio
async def test_route_ecom_does_not_duplicate_existing_task_url():
    llm = _mock_llm({
        "intent": "run_now",
        "args": {
            "worker_id": "b1",
            "skill": "ecom-best-source",
            "task": "要3 https://item.jd.com/10159624742014.html",
        },
    })
    result = await route(
        "@开票助手1号 [六神花露水 - 京东](https://item.jd.com/10159624742014.html) 要3",
        llm,
    )
    assert result.args["task"] == "要3 https://item.jd.com/10159624742014.html"


@pytest.mark.asyncio
async def test_route_ecom_recovers_unknown_when_message_contains_jd_url():
    llm = _mock_llm({"intent": "unknown", "args": {}})
    text = "@开票助手1号 [六神花露水 - 京东](https://item.jd.com/10159624742014.html) 要3"
    result = await route(text, llm)
    assert result.intent == Intent.RUN_NOW
    assert result.args == {
        "worker_id": "b1",
        "skill": "ecom-best-source",
        "task": text,
    }


@pytest.mark.asyncio
async def test_route_unknown_non_jd_url_stays_unknown():
    llm = _mock_llm({"intent": "unknown", "args": {}})
    result = await route("看一下 https://example.com/foo", llm)
    assert result.intent == Intent.UNKNOWN
    assert result.args == {}


@pytest.mark.asyncio
async def test_route_ecom_recovers_freeform_when_message_contains_jd_url():
    llm = _mock_llm({
        "intent": "freeform",
        "args": {
            "worker_id": "b1",
            "task": "六神花露水 https://item.jd.com/10159624742014.html 要3",
        },
    })
    text = "@开票助手1号 [六神花露水 - 京东](https://item.jd.com/10159624742014.html) 要3"
    result = await route(text, llm)
    assert result.intent == Intent.RUN_NOW
    assert result.args == {
        "worker_id": "b1",
        "skill": "ecom-best-source",
        "task": text,
    }


@pytest.mark.asyncio
async def test_route_ecom_recovery_keeps_worker_from_freeform_args():
    llm = _mock_llm({
        "intent": "freeform",
        "args": {"worker_id": "b3", "task": "https://item.jd.com/10159624742014.html 要3"},
    })
    result = await route("https://item.jd.com/10159624742014.html 要3", llm)
    assert result.intent == Intent.RUN_NOW
    assert result.args["worker_id"] == "b3"


@pytest.mark.asyncio
async def test_route_schedule_add():
    llm = _mock_llm({
        "intent": "schedule_add",
        "args": {"cron": "0 16 * * *", "worker_id": "b2", "skill": "fapiao-1688"},
    })
    result = await route("每天 16:00 让 b2 跑 fapiao-1688", llm)
    assert result.intent == Intent.SCHEDULE_ADD
    assert result.args["cron"] == "0 16 * * *"


@pytest.mark.asyncio
async def test_route_schedule_list():
    llm = _mock_llm({"intent": "schedule_list", "args": {}})
    result = await route("看定时任务", llm)
    assert result.intent == Intent.SCHEDULE_LIST


@pytest.mark.asyncio
async def test_route_schedule_remove():
    llm = _mock_llm({"intent": "schedule_remove", "args": {"entry_id": 3}})
    result = await route("删掉 #3", llm)
    assert result.intent == Intent.SCHEDULE_REMOVE
    assert result.args == {"entry_id": 3}


@pytest.mark.asyncio
async def test_route_skill_list():
    llm = _mock_llm({"intent": "skill_list", "args": {}})
    result = await route("有什么 skill", llm)
    assert result.intent == Intent.SKILL_LIST


@pytest.mark.asyncio
async def test_route_freeform():
    llm = _mock_llm({"intent": "freeform", "args": {"worker_id": "b2", "task": "去京东刷购物车"}})
    result = await route("b2 去京东刷购物车", llm)
    assert result.intent == Intent.FREEFORM
    assert result.args == {"worker_id": "b2", "task": "去京东刷购物车"}


@pytest.mark.asyncio
async def test_route_json_embedded_in_text():
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(
        return_value=ChatResponse(
            text='Here is my answer: {"intent": "query_status", "args": {}} done',
            tool_calls=[],
            finish_reason="stop",
        )
    )
    result = await route("查状态", llm)
    assert result.intent == Intent.QUERY_STATUS


# ── Task #7: conversation buffer ─────────────────────────────────────────────

def test_conversation_buffer_append_and_recent():
    from agent.conversation import ConversationBuffer
    buf = ConversationBuffer(max_per_chat=3)
    buf.append("chat1", "查状态", "query_status", "Worker 状态")
    buf.append("chat1", "b3 跑 fapiao-1688", "run_now", "已派发")
    turns = buf.recent("chat1")
    assert len(turns) == 2
    assert turns[0] == ("查状态", "query_status", "Worker 状态")
    assert turns[1] == ("b3 跑 fapiao-1688", "run_now", "已派发")


def test_conversation_buffer_max_per_chat():
    from agent.conversation import ConversationBuffer
    buf = ConversationBuffer(max_per_chat=2)
    buf.append("c", "a", "unknown", "A")
    buf.append("c", "b", "unknown", "B")
    buf.append("c", "c", "unknown", "C")
    turns = buf.recent("c")
    assert len(turns) == 2
    assert turns[0][0] == "b"
    assert turns[1][0] == "c"


def test_conversation_buffer_separate_chats():
    from agent.conversation import ConversationBuffer
    buf = ConversationBuffer()
    buf.append("chat1", "text1", "help", "Help")
    buf.append("chat2", "text2", "unknown", "?")
    assert buf.recent("chat1")[0][0] == "text1"
    assert buf.recent("chat2")[0][0] == "text2"


def test_conversation_buffer_empty_chat():
    from agent.conversation import ConversationBuffer
    buf = ConversationBuffer()
    assert buf.recent("never_used") == []


@pytest.mark.asyncio
async def test_nlu_with_recent_context():
    """Prompt sent to LLM includes recent_turns context block."""
    captured_messages: list[list[dict]] = []

    llm = MagicMock(spec=LLMClient)

    async def fake_chat(messages, tools=None):
        captured_messages.append(messages)
        return ChatResponse(
            text=json.dumps({"intent": "run_now", "args": {"worker_id": "b3", "skill": "fapiao-1688"}}),
            tool_calls=[],
            finish_reason="stop",
        )

    llm.chat = fake_chat

    recent = [
        ("b3 跑 fapiao-1688", "run_now", "[pc-test] 已派发"),
    ]
    result = await route("再跑一次刚才那个", llm, recent_turns=recent)
    assert result.intent == Intent.RUN_NOW

    assert len(captured_messages) == 1
    user_msg = captured_messages[0][1]["content"]
    assert "Recent conversation" in user_msg
    assert "b3 跑 fapiao-1688" in user_msg
    assert "再跑一次刚才那个" in user_msg


@pytest.mark.asyncio
async def test_nlu_without_recent_context_no_prefix():
    """Without recent_turns, prompt content is just the text (no prefix block)."""
    captured_messages: list[list[dict]] = []

    llm = MagicMock(spec=LLMClient)

    async def fake_chat(messages, tools=None):
        captured_messages.append(messages)
        return ChatResponse(
            text=json.dumps({"intent": "query_status", "args": {}}),
            tool_calls=[],
            finish_reason="stop",
        )

    llm.chat = fake_chat

    result = await route("查状态", llm, recent_turns=None)
    assert result.intent == Intent.QUERY_STATUS

    user_msg = captured_messages[0][1]["content"]
    assert "Recent conversation" not in user_msg
    assert user_msg == "查状态"
