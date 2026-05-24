from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.knowledge_sink import consolidate_topic, run_daily_consolidation
from agent.knowledge_store import KnowledgeStore
from agent.llm_client import ChatResponse, LLMClient


def _mock_llm(merged_text: str) -> LLMClient:
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(
        return_value=ChatResponse(
            text=merged_text,
            tool_calls=[],
            finish_reason="stop",
        )
    )
    return llm


def _mock_llm_empty() -> LLMClient:
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(
        return_value=ChatResponse(
            text="",
            tool_calls=[],
            finish_reason="stop",
        )
    )
    return llm


def _mock_llm_error() -> LLMClient:
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    return llm


# ── consolidate_topic ─────────────────────────────────────────────────────────

async def test_consolidate_topic_writes_curated(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "shadow-dom", "## 初始\n每次加购触发滑块")
    merged = "---\ntopic: shadow-dom\nversion: 1\n---\n## 合并内容\n"
    llm = _mock_llm(merged)

    updated = await consolidate_topic("shadow-dom", store, llm)

    assert updated is True
    curated = store.load_curated("shadow-dom")
    assert curated is not None
    assert "version: 1" in curated


async def test_consolidate_topic_no_machine_views_returns_false(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    llm = _mock_llm("anything")

    updated = await consolidate_topic("nonexistent", store, llm)

    assert updated is False
    llm.chat.assert_not_called()


async def test_consolidate_topic_empty_llm_response_returns_false(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "topic-x", "some notes")
    llm = _mock_llm_empty()

    updated = await consolidate_topic("topic-x", store, llm)

    assert updated is False
    assert store.load_curated("topic-x") is None


async def test_consolidate_topic_merges_multiple_machines(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "1688-tips", "江湖提示 A")
    store.append("pc-beijing", "1688-tips", "北京提示 B")
    merged = "---\ntopic: 1688-tips\nversion: 1\n---\n合并后内容"
    llm = _mock_llm(merged)

    updated = await consolidate_topic("1688-tips", store, llm)

    assert updated is True
    called_messages = llm.chat.call_args[1]["messages"]
    prompt_text = called_messages[0]["content"]
    assert "pc-jianghu" in prompt_text
    assert "pc-beijing" in prompt_text
    assert "1688-tips" in prompt_text


async def test_consolidate_topic_includes_existing_curated_in_prompt(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "my-topic", "新观察")
    store.write_curated("my-topic", "---\ntopic: my-topic\nversion: 2\n---\n旧 curated")
    llm = _mock_llm("---\ntopic: my-topic\nversion: 3\n---\n新合并")

    await consolidate_topic("my-topic", store, llm)

    prompt_text = llm.chat.call_args[1]["messages"][0]["content"]
    assert "旧 curated" in prompt_text


# ── run_daily_consolidation ───────────────────────────────────────────────────

async def test_run_daily_consolidation_happy_path(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "topic-a", "内容 A")
    store.append("pc-beijing", "topic-b", "内容 B")
    merged_a = "---\ntopic: topic-a\nversion: 1\n---\n合并 A"
    merged_b = "---\ntopic: topic-b\nversion: 1\n---\n合并 B"

    call_count = [0]
    responses = [merged_a, merged_b]

    async def _fake_chat(messages, tools=None):
        r = responses[call_count[0] % len(responses)]
        call_count[0] += 1
        return ChatResponse(text=r, tool_calls=[], finish_reason="stop")

    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(side_effect=_fake_chat)

    results = await run_daily_consolidation(store, llm)

    assert "topic-a" in results
    assert "topic-b" in results
    assert results["topic-a"] is True
    assert results["topic-b"] is True


async def test_run_daily_consolidation_llm_error_per_topic(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "good-topic", "good content")
    store.append("pc-jianghu", "bad-topic", "bad content")

    call_count = [0]

    async def _mixed_chat(messages, tools=None):
        prompt = messages[0]["content"]
        call_count[0] += 1
        if "bad-topic" in prompt:
            raise RuntimeError("simulated LLM failure")
        return ChatResponse(
            text="---\ntopic: good-topic\nversion: 1\n---\nOK",
            tool_calls=[],
            finish_reason="stop",
        )

    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(side_effect=_mixed_chat)

    results = await run_daily_consolidation(store, llm)

    assert results["good-topic"] is True
    assert results["bad-topic"] is False


async def test_run_daily_consolidation_empty_store(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    llm = _mock_llm("anything")

    results = await run_daily_consolidation(store, llm)

    assert results == {}
    llm.chat.assert_not_called()


# ── config integration ────────────────────────────────────────────────────────

def test_knowledge_config_defaults() -> None:
    from agent.config import KnowledgeConfig
    cfg = KnowledgeConfig()
    assert cfg.enabled is True
    assert cfg.is_merger is False
    assert cfg.consolidate_cron == "0 2 * * *"
    assert str(cfg.root) == "knowledge"


def test_knowledge_config_from_dict(tmp_path: Path) -> None:
    from agent.config import _knowledge_config_from_dict
    knowledge_root = tmp_path / "knowledge"
    d = {
        "enabled": True,
        "root": str(knowledge_root),
        "is_merger": True,
        "consolidate_cron": "0 3 * * *",
    }
    cfg = _knowledge_config_from_dict(d)
    assert cfg.is_merger is True
    assert cfg.consolidate_cron == "0 3 * * *"


def test_knowledge_config_rejects_relative_root() -> None:
    from agent.config import _knowledge_config_from_dict
    with pytest.raises(ValueError, match="absolute"):
        _knowledge_config_from_dict({"root": "./knowledge"})


def test_knowledge_config_rejects_zero_interval(tmp_path: Path) -> None:
    from agent.config import _knowledge_config_from_dict
    with pytest.raises(ValueError, match="> 0"):
        _knowledge_config_from_dict({"root": str(tmp_path), "pull_interval_secs": 0})


def test_master_config_has_knowledge_field() -> None:
    from agent.config import KnowledgeConfig, MasterConfig
    mc = MasterConfig(workers=[])
    assert isinstance(mc.knowledge, KnowledgeConfig)
    assert mc.knowledge.enabled is True
