from __future__ import annotations

import copy
import time
import json
from types import SimpleNamespace

import pytest

from agent.ecom_workflow import merge_product, retrieve_qualified_candidates
from data_sources import _normalize_mcp_detail_response, _normalize_onebound_detail, merge_candidates
from ecom_config import EcomConfig
from fetch_candidates import fetch_candidates
from product_match import sku_rows
from sourcing_pipeline import select_confirmation_batch, _merge_confirmed_detail, run_from_files
from sourcing_rules import run_pipeline


def target(*, vision: bool = False) -> dict:
    return {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干", "selected_sku": "草莓100g×6袋", "buy_multiple": 3, "jd_price": 30, "require_vision": vision}


def candidate(offer_id: str, shop: str | None = None) -> dict:
    return {"num_iid": offer_id, "title": "样例牌草莓饼干100g×6袋", "shopName": shop or "供应商" + offer_id, "compositeScore": 5, "shopYear": 5}


def detail(offer_id: str, *, correct: bool = True) -> dict:
    return {"fetched_at": time.time(), "skus": {"sku": [{"sku_id": offer_id, "properties_name": ("草莓" if correct else "巧克力") + "100g×6袋", "price": 12, "quantity": 100, "sku_image_url": "https://img.example/" + offer_id + ".jpg"}]}}


class FakeVision:
    def __init__(self, *, wrong_ids=(), per_call: int = 100):
        self.metrics = {"verified_skus": 0, "calls": 0, "cache_hits": 0, "errors": 0}
        self.wrong_ids = set(wrong_ids)
        self.per_call = per_call
        self.budgets = []

    async def verify_candidates(self, wanted, candidates, max_pairs):
        self.budgets.append(max_pairs)
        rows = [(item, row) for item in candidates for row in sku_rows(item.get("detail") or {}) if not row.get("vision")]
        chosen = rows[:min(max_pairs, self.per_call)]
        for item, row in chosen:
            row["vision"] = {"status": "mismatch" if item["num_iid"] in self.wrong_ids else "matched"}
        self.metrics["verified_skus"] += len(chosen)
        return {"attempted": len(chosen), "remaining": len(rows) - len(chosen)}


def install_data(monkeypatch, *, correct_ids=()):
    calls = []
    class FakeData:
        cfg = SimpleNamespace(runtime={})
        stats = {}
        def item_get(self, offer_id):
            calls.append(offer_id)
            return detail(offer_id, correct=offer_id in correct_ids)
        def close(self):
            pass
    monkeypatch.setattr("sourcing_pipeline._make_data_client", FakeData)
    return calls


def fetched(items: list[dict]) -> dict:
    return {"query": "样例牌 草莓 饼干 100g 6袋", "extra_queries": [], "image_urls": [], "candidates": items, "errors": [], "stats": {}}


@pytest.mark.asyncio
async def test_correct_skus_beyond_first_24_are_confirmed(monkeypatch):
    calls = install_data(monkeypatch, correct_ids={str(i) for i in range(25, 31)})
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", lambda *args: fetched([]))
    items = [candidate(str(i)) for i in range(1, 31)]
    for item in items[24:]:
        item["title"] = "样例牌饼干多规格"
    payload, report = await retrieve_qualified_candidates(target(), fetched(items), EcomConfig("hybrid", {}, {}), FakeVision())
    assert {row["num_iid"] for row in run_pipeline(payload)["final"]} == {str(i) for i in range(25, 31)}
    assert len(calls) == 30
    assert report["stop_reason"] == "enough_qualified_suppliers"
    assert all(row["detail_attempts"] <= 10 for row in report["rounds"])


def test_first_batch_reserves_room_for_other_suppliers():
    items = [candidate(str(i), "重复供应商") for i in range(24)] + [candidate("new" + str(i)) for i in range(6)]
    batch = select_confirmation_batch(items, target(), 10, 2)
    assert sum(item["shopName"] == "重复供应商" for item in batch) == 2
    assert len({item["shopName"] for item in batch}) == 7


@pytest.mark.asyncio
async def test_nonempty_but_wrong_primary_triggers_secondary_source(monkeypatch):
    calls = install_data(monkeypatch, correct_ids={"good" + str(i) for i in range(6)})
    searches = []
    def search(*args):
        searches.append(args)
        return fetched([candidate("good" + str(i)) for i in range(6)] if args[7] == "onebound" else [])
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", search)
    payload, report = await retrieve_qualified_candidates(target(), fetched([candidate("wrong")]), EcomConfig("hybrid", {}, {}), FakeVision())
    assert len(run_pipeline(payload)["final"]) == 6
    assert searches[0][7] == "onebound"
    assert "wrong" in calls and "good0" in calls
    assert report["search_rounds"][0]["kind"] == "secondary_source"


@pytest.mark.asyncio
async def test_expands_to_next_page_when_rewrites_find_nothing(monkeypatch):
    install_data(monkeypatch, correct_ids={"p2-" + str(i) for i in range(6)})
    searches = []
    def search(*args):
        searches.append(args)
        return fetched([candidate("p2-" + str(i)) for i in range(6)] if args[9] == 2 else [])
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", search)
    payload, report = await retrieve_qualified_candidates(target(), fetched([candidate("wrong")]), EcomConfig("onebound", {}, {}), FakeVision())
    assert len(run_pipeline(payload)["final"]) == 6
    assert any(args[9] == 2 for args in searches)
    assert report["stop_reason"] == "enough_qualified_suppliers"


@pytest.mark.asyncio
async def test_visual_rejection_continues_to_later_candidates(monkeypatch):
    ids = {str(i) for i in range(30)}
    install_data(monkeypatch, correct_ids=ids)
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", lambda *args: fetched([]))
    vision = FakeVision(wrong_ids={str(i) for i in range(24)})
    payload, report = await retrieve_qualified_candidates(target(vision=True), fetched([candidate(str(i)) for i in range(30)]), EcomConfig("onebound", {}, {}, vision={"max_pairs": 40}), vision)
    assert len(run_pipeline(payload)["final"]) == 6
    assert report["vision_attempts"] == 30
    assert report["stop_reason"] == "enough_qualified_suppliers"


@pytest.mark.asyncio
async def test_total_visual_budget_is_honored_across_rounds(monkeypatch):
    install_data(monkeypatch, correct_ids={str(i) for i in range(30)})
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", lambda *args: fetched([]))
    vision = FakeVision(wrong_ids={str(i) for i in range(30)})
    payload, report = await retrieve_qualified_candidates(target(vision=True), fetched([candidate(str(i)) for i in range(30)]), EcomConfig("onebound", {}, {}, vision={"max_pairs": 12}), vision)
    assert report["vision_attempts"] == 12
    assert vision.budgets == [12, 2]
    assert report["stop_reason"] == "vision_budget_exhausted"
    assert payload["partial"]


@pytest.mark.asyncio
async def test_visual_pending_skus_continue_without_new_detail(monkeypatch):
    calls = install_data(monkeypatch, correct_ids={"a"})
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", lambda *args: fetched([]))
    item = candidate("a")
    item["detail"] = detail("a")
    item["detail"]["skus"]["sku"] += [{**copy.deepcopy(item["detail"]["skus"]["sku"][0]), "sku_id": str(i)} for i in range(3)]
    item["_retrieval_checked"] = True
    vision = FakeVision(per_call=1)
    _, report = await retrieve_qualified_candidates(target(vision=True), fetched([item]), EcomConfig("onebound", {}, {}, runtime={"retrieval_max_search_rounds": 0}), vision)
    assert not calls
    assert report["vision_attempts"] == 4
    assert len(vision.budgets) == 4


@pytest.mark.asyncio
async def test_resume_keeps_budget_and_resumes_unverified_details(monkeypatch):
    calls = install_data(monkeypatch, correct_ids={"resume"})
    item = candidate("resume")
    item.update({"detail": detail("resume"), "_retrieval_checked": True})
    state = fetched([item])
    state["retrieval"] = {"vision_attempts": 2, "detail_attempts": 1}
    vision = FakeVision()
    payload, report = await retrieve_qualified_candidates(target(vision=True), state, EcomConfig("onebound", {}, {}, runtime={"retrieval_max_search_rounds": 0}, vision={"max_pairs": 3}), vision)
    assert not calls
    assert report["vision_attempts"] == 3
    assert report["detail_attempts"] == 1
    assert len(run_pipeline(payload)["final"]) == 1
    assert vision.budgets == [1]


@pytest.mark.asyncio
async def test_refresh_changed_image_rechecks_using_remaining_budget(monkeypatch):
    calls = install_data(monkeypatch, correct_ids={str(i) for i in range(6)})
    state = fetched([candidate(str(i)) for i in range(6)])
    vision = FakeVision()
    cfg = EcomConfig("onebound", {}, {}, runtime={"retrieval_max_search_rounds": 0}, vision={"max_pairs": 7})
    payload, report = await retrieve_qualified_candidates(target(vision=True), state, cfg, vision)
    assert report["qualified_suppliers"] == 6 and report["vision_attempts"] == 6
    item = payload["candidates"][0]
    refreshed = detail(item["num_iid"])
    refreshed["skus"]["sku"][0]["sku_image_url"] = "https://img.example/changed.jpg"
    _merge_confirmed_detail(item, refreshed)
    assert "vision" not in sku_rows(item["detail"])[0]
    payload, report = await retrieve_qualified_candidates(target(vision=True), state, cfg, vision)
    assert len(calls) == 6
    assert report["vision_attempts"] == 7
    assert report["qualified_suppliers"] == 6
    assert report["stop_reason"] == "enough_qualified_suppliers"
    assert vision.budgets[-1] == 1


@pytest.mark.asyncio
async def test_detail_budget_reports_incomplete_search(monkeypatch):
    calls = install_data(monkeypatch)
    monkeypatch.setattr("agent.ecom_workflow.fetch_candidates", lambda *args: fetched([]))
    payload, report = await retrieve_qualified_candidates(target(), fetched([candidate(str(i)) for i in range(30)]), EcomConfig("onebound", {}, {}, runtime={"retrieval_max_details": 2}), FakeVision())
    assert len(calls) == 2
    assert report["stop_reason"] == "detail_budget_exhausted"
    assert report["unexamined_count"] == 28 and payload["partial"]


def test_budget_stop_reason_is_exported_as_partial_completion(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"target": target(), "candidates": [], "partial": True, "retrieval": {"stop_reason": "detail_budget_exhausted", "detail_attempts": 80}}), encoding="utf-8")
    summary = run_from_files(jd_product_path=None, candidates_path=None, merged_input_path=str(source), output_path=str(tmp_path / "result.csv"), confirm_details=False)
    assert summary["status"] == "部分完成"
    assert summary["retrieval"]["stop_reason"] == "detail_budget_exhausted"


def test_exact_page_range_and_search_provenance(monkeypatch):
    calls = []
    class FakeData:
        cfg = SimpleNamespace(runtime={})
        stats = {}
        def search(self, query, **kwargs):
            calls.append(kwargs["page"])
            return [{**candidate("a"), "provider": "onebound", "sources": ["text"]}]
        def close(self): pass
    monkeypatch.setattr("fetch_candidates.make_data_client", lambda mode: FakeData())
    result = fetch_candidates("brand", [], [], 1, 0, 50, 0, "onebound", target(), 3)
    assert calls == [3]
    assert result["candidates"][0]["retrieval_hits"][0] == {"channel": "text", "query": "brand", "page": 3, "rank": 1, "provider": "onebound"}
    other = copy.deepcopy(result["candidates"][0])
    other["retrieval_hits"][0]["query"] = "rewritten"
    assert len(merge_candidates([result["candidates"][0], other])[0]["retrieval_hits"]) == 2


def test_sku_extra_images_remain_bound_to_their_row():
    item = {"item_imgs": [{"url": "shared.jpg"}], "skus": {"sku": [{"sku_id": "a", "sku_image_url": "front.jpg", "image_urls": ["back.jpg"]}, {"sku_id": "b"}]}}
    rows = sku_rows(_normalize_onebound_detail(item))
    assert rows[0]["sku_image_urls"] == ["front.jpg", "back.jpg"]
    assert not rows[1].get("sku_image_urls")
    normalized = _normalize_mcp_detail_response({"result": {"originImageUrls": ["shared.jpg"], "productSkuInfos": [{"skuId": "a", "skuImageUrl": "front.jpg", "skuImageUrls": ["back.jpg"]}, {"skuId": "b"}]}})
    assert sku_rows(normalized)[0]["sku_image_urls"] == ["front.jpg", "back.jpg"]
    assert sku_rows(normalized)[1]["sku_image_urls"] == []


def test_static_images_are_merged_with_their_evidence_only():
    browser = {"item_id": "a", "main_image_url": "browser.jpg", "image_urls": ["browser.jpg"], "image_urls_scope": "selected_sku"}
    static = {"item_id": "a", "main_image_url": "static.jpg", "image_urls": ["static.jpg", "back.jpg"], "image_urls_scope": "product_page", "image_evidence": [{"url": "static.jpg", "source": "static"}]}
    assert merge_product(browser, static)["image_urls"] == ["browser.jpg"]
    merged = merge_product({"item_id": "a"}, static)
    assert merged["image_urls_scope"] == "product_page" and merged["image_evidence"] == static["image_evidence"]
    assert merge_product({"item_id": "b"}, static)["target_errors"]
    assert "main_image_url" not in merge_product({"item_id": "a"}, {**static, "target_errors": ["conflict"]})
