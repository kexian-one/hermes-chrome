from __future__ import annotations

import copy
import json
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import ecom_workflow as workflow
from agent.config import LLMSettings, WorkerConfig
from agent.llm_client import ChatResponse
from agent.product_vision import ProductVision
from agent.artifacts import validate_sourcing_output
from ecom_config import EcomConfig
from sourcing_pipeline import _merge_confirmed_detail
from data_sources import _MCPSessionRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["matched", "wrong_flavor", "vision_timeout", "detail_failure"])
async def test_workflow_offline_from_task_to_csv(tmp_path, monkeypatch, mode):
    output = tmp_path / "outputs" / "run"
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output))
    monkeypatch.delenv("WORKER_RESUME_DIR", raising=False)
    cfg = EcomConfig("hybrid", {}, {}, runtime={"task_timeout_seconds": 10}, vision={"enabled": True})
    monkeypatch.setattr(workflow, "load_ecom_config", lambda root: cfg)
    attrs = {"brand": "样例牌", "category": "饼干", "flavor": "草莓", "net_content": "100g", "pack_count": 6, "packaging": "袋装"}
    product = {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "item_id": "123456", "selected_sku": "草莓100g×6袋", "main_image_url": "https://img.example/target.jpg", "jd_url": "https://item.jd.com/123456.html"}
    class NoBrowser:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): raise OSError("offline fixture")
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(workflow, "OpenClaudeInChromeClient", NoBrowser)
    monkeypatch.setattr(workflow, "fetch_product", lambda *args: copy.deepcopy(product))
    candidate = {"num_iid": "1", "title": "样例牌饼干", "shopName": "模拟供应商", "compositeScore": 5, "shopYear": 5}
    monkeypatch.setattr(workflow, "fetch_candidates", lambda *args: {"candidates": [copy.deepcopy(candidate)], "stats": {}, "errors": []})
    class FakeData:
        stats = {}
        def item_get(self, offer_id):
            if mode == "detail_failure": raise TimeoutError()
            return {"fetched_at": time.time(), "skus": {"sku": [
                {"sku_id": "cheap-wrong", "properties_name": "巧克力100g×6袋", "price": 1, "quantity": 99},
                {"sku_id": "correct", "properties_name": "草莓100g×6袋", "price": 12, "quantity": 99, "sku_image_url": "https://img.example/candidate.jpg"}]}}
        def close(self): pass
    monkeypatch.setattr("sourcing_pipeline._make_data_client", FakeData)
    text_llm = SimpleNamespace(structured=AsyncMock(return_value=ChatResponse(text=json.dumps({"quantity": 3, "user_attributes": {}, "allow_pack_substitution": False}), tool_calls=[], finish_reason="stop")))
    async def vision_response(prompt, data, images):
        current = dict(attrs)
        if data.get("operation") == "read_product_label":
            if "candidate" in data["image_sha256"]:
                if mode == "vision_timeout": raise TimeoutError()
                if mode == "wrong_flavor": current["flavor"] = "巧克力"
            return ChatResponse(text=json.dumps({"product_visible": True, "readability": "clear", "attributes": current, "evidence": {k: str(v) for k, v in current.items()}, "identifiers": []}), tool_calls=[], finish_reason="stop")
        if "candidate" in data:
            if mode == "vision_timeout": raise TimeoutError()
            if mode == "wrong_flavor": current["flavor"] = "巧克力"
        return ChatResponse(text=json.dumps({"attributes": current, "evidence": {k: str(v) for k, v in current.items()}, "status": "matched", "conflicts": []}), tool_calls=[], finish_reason="stop")
    vision_llm = SimpleNamespace(_model="qwen3.8-flash", structured=AsyncMock(side_effect=vision_response))
    monkeypatch.setattr(workflow, "ProductVision", lambda llm, path, concurrency, timeout, **kwargs: ProductVision(llm, path, concurrency, timeout, image_loader=AsyncMock(side_effect=lambda url: ("data:image/png;base64,AAAA", url)), **kwargs))
    settings = LLMSettings("http://invalid", "qwen3.8-flash", "fixture")
    wc = WorkerConfig("b1", 18765, settings, settings)
    assert await workflow.run_sourcing(wc, "找货草莓100g×6袋，采购3件 https://item.jd.com/123456.html", text_llm, vision_llm) == 0
    manifest = validate_sourcing_output(output / "找货结果.csv")
    assert manifest and manifest["final_count"] == (1 if mode == "matched" else 0)
    report = json.loads((output / ".ecom-scratch" / "result.json").read_text(encoding="utf-8"))
    if mode == "matched":
        assert report["final"][0]["selected_sku"]["sku_id"] == "correct"
        assert report["final"][0]["selected_sku"]["purchase_total"] == 36
    if mode in {"vision_timeout", "detail_failure"}:
        assert manifest["status"] == "部分完成" and manifest["pending_count"] == 1
    metrics = json.loads((output / ".ecom-scratch" / "metrics.json").read_text())
    assert set(metrics["stages"]) >= {"target", "search", "details", "vision", "refresh"}
    assert text_llm.structured.await_count == 1


def test_refresh_keeps_vision_only_for_unchanged_sku_evidence():
    row = {"sku_id": "a", "name": "草莓100g×6袋", "price": 2, "quantity": 3, "sku_image_url": "a.jpg", "vision": {"status": "matched"}}
    candidate = {"title": "样例牌", "detail": {"skus": {"sku": [row]}}}
    fresh = {"skus": {"sku": [{k: v for k, v in row.items() if k != "vision"}]}}
    fresh["skus"]["sku"][0]["price"] = 5
    _merge_confirmed_detail(candidate, copy.deepcopy(fresh))
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "matched"
    fresh["skus"]["sku"][0]["name"] = "巧克力100g×6袋"
    _merge_confirmed_detail(candidate, fresh)
    assert "vision" not in candidate["detail"]["skus"]["sku"][0]


def test_mcp_session_enters_and_exits_in_same_task(monkeypatch):
    events = []
    @asynccontextmanager
    async def transport(*args, **kwargs):
        owner = asyncio.current_task()
        yield (None, None)
        assert asyncio.current_task() is owner
        events.append("closed")
    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): self.owner = asyncio.current_task(); return self
        async def __aexit__(self, *args): assert asyncio.current_task() is self.owner
        async def initialize(self): pass
        async def call_tool(self, *args): return SimpleNamespace(isError=False, structuredContent={"ok": True})
    monkeypatch.setattr("mcp.client.sse.sse_client", transport)
    monkeypatch.setattr("mcp.client.session.ClientSession", Session)
    runner = _MCPSessionRunner(lambda: "http://invalid", 1)
    try:
        assert runner.call_tool("fixture", {}) == {"ok": True}
    finally:
        runner.close()
    assert events == ["closed"]
    assert not runner._thread.is_alive()


@pytest.mark.asyncio
async def test_exact_commands_skip_model_and_other_user_history(monkeypatch):
    from agent.nlu import route, Intent
    from agent.bot import _fetch_chat_context
    llm = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("model must not run")))
    assert (await route("查状态", llm)).intent == Intent.QUERY_STATUS
    message = lambda sender, text: SimpleNamespace(message_id=text, create_time=str(int(time.time() * 1000)), sender=SimpleNamespace(sender_type="user", id=sender), msg_type="text", body=SimpleNamespace(content=json.dumps({"text": text})))
    channel = SimpleNamespace(client=SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(alist=AsyncMock(return_value=SimpleNamespace(data=SimpleNamespace(items=[message("u2", "巧克力500g"), message("u1", "草莓100g")]))))))))
    assert await _fetch_chat_context(channel, "group", "now", "bot", "u1") == [("草莓100g", "", "")]


def test_onebound_property_images_and_enrichment_are_sku_bound():
    from data_sources import _normalize_onebound_detail, _merge_detail_enrichment
    original = {"prop_imgs": {"prop_img": [{"properties": "1:2", "url": "strawberry.jpg"}]}, "skus": {"sku": [{"sku_id": "a", "properties": "1:2;3:4", "price": None, "quantity": 0}, {"sku_id": "b", "properties": "1:9;3:4"}]}}
    detail = _normalize_onebound_detail(original)
    assert detail["skus"]["sku"][0]["sku_image_url"] == "strawberry.jpg"
    assert "sku_image_url" not in detail["skus"]["sku"][1]
    other = {"skus": {"sku": [{"sku_id": "b", "price": 99}, {"sku_id": "a", "price": 12, "quantity": 100}]}}
    enriched = _merge_detail_enrichment(detail, other)
    assert enriched["skus"]["sku"][0]["price"] == 12
    assert enriched["skus"]["sku"][0]["quantity"] == 0
    assert original["skus"]["sku"][0]["price"] is None


def test_parallel_duplicate_requests_share_one_fetch(tmp_path, monkeypatch):
    from data_sources import OneboundClient
    from runtime_support import parallel_map
    calls = []
    def get_json(*args):
        calls.append(1)
        time.sleep(.03)
        return {"item": {"title": "fixture"}}
    monkeypatch.setattr("data_sources._http_get_json", get_json)
    client = OneboundClient(EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}), tmp_path)
    results = parallel_map([1, 1, 1], lambda _: client.item_get("1"), concurrency=3, timeout=2)
    assert all(r["ok"] for r in results)
    assert len(calls) == 1 and client.stats.cache_hits + client.stats.coalesced_hits == 2


def test_labelled_replay_measures_false_positives():
    from scripts.evaluate_sourcing import evaluate
    cases = json.loads((Path(__file__).parent / "fixtures" / "sourcing-labels.json").read_text(encoding="utf-8"))["cases"]
    correct = evaluate(cases)
    assert correct["true_positive"] == 1 and correct["false_positive"] == 0
    cases[0]["expected_matches"] = []
    wrong = evaluate(cases)
    assert wrong["false_positive"] == 1 and wrong["precision"] == 0


@pytest.mark.asyncio
async def test_queue_recovers_origin_and_pause_after_restart(tmp_path):
    from agent.master import _MasterDispatcherImpl
    from agent.config import MasterConfig
    from agent.worker_state import WorkerStateTracker
    from agent.schedule_store import ScheduleStore
    from agent.channels import ReplyTarget
    settings = LLMSettings("http://invalid", "fixture", "fixture")
    cfg = MasterConfig([WorkerConfig("b1", 18765, settings, settings)], project_root=tmp_path, log_dir=tmp_path)
    store = ScheduleStore(tmp_path / "schedule.yaml")
    state_path = tmp_path / "workers.json"
    first = _MasterDispatcherImpl(cfg, WorkerStateTracker(state_path), store, [True])
    channel = SimpleNamespace(app_id="fixture-app")
    task = "只要草莓100g×6袋，采购3件 https://item.jd.com/123456.html"
    await first.spawn_now(None, "ecom-best-source", ReplyTarget(channel, "fixture-chat"), task=task)
    restored = _MasterDispatcherImpl(cfg, WorkerStateTracker(state_path), store, [False])
    assert restored._paused[0] is True
    restored.register_channel(channel, "", supports_files=True)
    await asyncio.sleep(0)
    queued = list(restored._global_queue)
    assert len(queued) == 1 and queued[0].task == task
    assert queued[0].reply_to.target_id == "fixture-chat"
    assert not restored._pending


@pytest.mark.asyncio
async def test_upload_false_receipt_is_failure(tmp_path):
    from agent.channels import ReplyTarget
    path = tmp_path / "result.csv"
    path.write_text("fixture")
    reply = ReplyTarget(SimpleNamespace(send_file=AsyncMock(return_value=False)), "fixture-chat", supports_files=True)
    assert not await reply.send_file(path)
