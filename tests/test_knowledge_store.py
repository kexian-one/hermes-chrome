from __future__ import annotations

from pathlib import Path

import pytest

from agent.knowledge_store import KnowledgeStore


def test_empty_store_list_topics(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    assert store.list_topics() == []


def test_empty_store_all_machine_topics(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    assert store.all_machine_topics() == []


def test_append_creates_file(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "1688-shadow-dom", "## 初始内容\n测试")
    f = tmp_path / "knowledge" / "by-machine" / "pc-jianghu" / "1688-shadow-dom.md"
    assert f.is_file()
    assert "初始内容" in f.read_text(encoding="utf-8")


def test_append_accumulates(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "topic-a", "第一条")
    store.append("pc-jianghu", "topic-a", "第二条")
    content = (tmp_path / "knowledge" / "by-machine" / "pc-jianghu" / "topic-a.md").read_text(
        encoding="utf-8"
    )
    assert "第一条" in content
    assert "第二条" in content


def test_load_curated_missing_returns_none(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    assert store.load_curated("nonexistent") is None


def test_write_and_load_curated(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.write_curated("1688-shadow-dom", "---\ntopic: 1688-shadow-dom\nversion: 1\n---\n内容")
    result = store.load_curated("1688-shadow-dom")
    assert result is not None
    assert "version: 1" in result


def test_list_machine_views_empty(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    assert store.list_machine_views("anything") == {}


def test_list_machine_views_multiple_machines(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "滑块触发条件", "江湖观察:每次加购触发")
    store.append("pc-beijing", "滑块触发条件", "北京观察:频繁切换账号触发")
    views = store.list_machine_views("滑块触发条件")
    assert set(views.keys()) == {"pc-jianghu", "pc-beijing"}
    assert "江湖观察" in views["pc-jianghu"]
    assert "北京观察" in views["pc-beijing"]


def test_list_machine_views_ignores_other_topics(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "topic-x", "内容 X")
    store.append("pc-jianghu", "topic-y", "内容 Y")
    views = store.list_machine_views("topic-x")
    assert "pc-jianghu" in views
    assert "topic-y" not in str(views)


def test_list_topics_union(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "topic-a", "a")
    store.append("pc-beijing", "topic-b", "b")
    store.write_curated("topic-c", "c")
    topics = store.list_topics()
    assert "topic-a" in topics
    assert "topic-b" in topics
    assert "topic-c" in topics


def test_all_machine_topics(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "shadow-dom", "a")
    store.append("pc-beijing", "滑块", "b")
    pairs = store.all_machine_topics()
    assert ("pc-beijing", "滑块") in pairs
    assert ("pc-jianghu", "shadow-dom") in pairs


def test_load_machine_missing_returns_none(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    assert store.load_machine("pc-jianghu", "nonexistent") is None


def test_load_machine_returns_content(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.append("pc-jianghu", "my-topic", "hello world")
    result = store.load_machine("pc-jianghu", "my-topic")
    assert result is not None
    assert "hello world" in result


def test_machine_cannot_overwrite_curated(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.write_curated("shared-topic", "curated content")
    store.append("pc-jianghu", "shared-topic", "machine content")
    curated = store.load_curated("shared-topic")
    assert curated == "curated content"


def test_write_curated_overwrites(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    store.write_curated("topic", "v1")
    store.write_curated("topic", "v2")
    assert store.load_curated("topic") == "v2"
