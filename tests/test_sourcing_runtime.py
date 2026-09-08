from __future__ import annotations

import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import pytest

from agent.ecom_workflow import _run_sync_stage, select_refresh_candidates
from data_sources import DataSourceStats, OneboundClient, _normalize_mcp_detail_response, make_data_client, release_data_client, task_data_session
from ecom_config import EcomConfig
from runtime_support import OperationCancelled, cancellation_scope, parallel_map, refresh_details, remaining_timeout
from sourcing_pipeline import _run_with_final_detail_confirmation, _merge_confirmed_detail


def wanted() -> dict:
    return {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干", "selected_sku": "草莓100g×6袋", "buy_multiple": 3, "jd_price": 100}


def offer(offer_id: str) -> dict:
    return {"num_iid": offer_id, "title": "样例牌草莓饼干100g×6袋", "shopName": "supplier-" + offer_id, "compositeScore": 5, "shopYear": 5}


def detail(offer_id: str) -> dict:
    return {"fetched_at": time.time(), "skus": {"sku": [{"sku_id": offer_id, "properties_name": "草莓100g×6袋", "price": 12, "quantity": 100}]}}


def test_stage_returns_on_deadline_and_keeps_fast_results():
    release = threading.Event()
    finished = threading.Event()
    def work(value):
        if value == "slow":
            release.wait(1)
            finished.set()
        return value
    started = time.monotonic()
    try:
        result = parallel_map(["slow", "fast"], work, concurrency=2, timeout=0.05)
        assert time.monotonic() - started < 0.4
        assert result[0]["ok"] is False and result[0]["error"] == "TimeoutError"
        assert result[1]["value"] == "fast"
        snapshot = copy.deepcopy(result)
    finally:
        release.set()
    assert finished.wait(1)
    assert result == snapshot


def test_stage_cancellation_stops_waiting_and_queued_operations():
    release = threading.Event()
    entered = threading.Event()
    started_items = []
    def work(value):
        started_items.append(value)
        entered.set()
        release.wait(1)
        return value
    with cancellation_scope() as cancellation:
        timer = threading.Timer(0.06, cancellation.cancel)
        timer.start()
        try:
            started = time.monotonic()
            result = parallel_map(list(range(10)), work, concurrency=1, timeout=5)
            assert time.monotonic() - started < 0.4
            assert entered.is_set() and started_items == [0]
            assert all(row["error"] == "OperationCancelled" for row in result)
        finally:
            release.set()
            timer.join()


def test_remaining_timeout_never_extends_a_small_budget():
    with cancellation_scope(0.01):
        assert 0 < remaining_timeout(30) <= 0.010001


def test_late_detail_thread_cannot_change_candidate(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    class FakeData:
        cfg = SimpleNamespace(runtime={"detail_timeout_seconds": 0.04})
        stats = {}
        def item_get(self, offer_id):
            release.wait(1)
            finished.set()
            return detail(offer_id)
        def close(self): pass
    monkeypatch.setattr("sourcing_pipeline._make_data_client", FakeData)
    candidate = offer("1")
    try:
        started = time.monotonic()
        result = _run_with_final_detail_confirmation({"target": wanted(), "candidates": [candidate]}, 6)
        assert time.monotonic() - started < 0.4
        assert result["confirmation"]["confirmed"] == 0
        assert candidate["detail_error"] == "TimeoutError"
        snapshot = copy.deepcopy(candidate)
    finally:
        release.set()
    assert finished.wait(1)
    time.sleep(0.02)
    assert candidate == snapshot and "detail" not in candidate


@pytest.mark.asyncio
async def test_cancelled_task_does_not_commit_late_detail(monkeypatch):
    release, entered, finished = threading.Event(), threading.Event(), threading.Event()
    class FakeData:
        cfg = SimpleNamespace(runtime={"detail_timeout_seconds": 5})
        stats = {}
        def item_get(self, offer_id):
            entered.set()
            release.wait(1)
            finished.set()
            return detail(offer_id)
        def close(self): pass
    monkeypatch.setattr("sourcing_pipeline._make_data_client", FakeData)
    candidate = offer("1")
    with cancellation_scope():
        task = asyncio.create_task(_run_sync_stage(_run_with_final_detail_confirmation, {"target": wanted(), "candidates": [candidate]}, 6))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    await asyncio.sleep(0.06)
    assert "detail" not in candidate and "detail_error" not in candidate


def test_task_reuses_provider_across_modes_and_closes_once(monkeypatch):
    created, closed = [], []
    class FakeProvider:
        def __init__(self, cfg):
            self.stats = DataSourceStats()
            created.append(self)
        def close(self):
            closed.append(self)
    monkeypatch.setattr("data_sources.OneboundClient", FakeProvider)
    monkeypatch.setattr("data_sources.AlphashopMCPClient", FakeProvider)
    monkeypatch.setenv("ALL_IN_AI_OFFLINE", "0")
    cfg = EcomConfig("hybrid", {}, {})
    with task_data_session(cfg) as session:
        first = make_data_client()
        assert make_data_client() is first
        secondary = make_data_client("onebound")
        assert secondary._ob is first._ob
        release_data_client(first)
        release_data_client(secondary)
        assert not closed
        assert session.stats()["onebound"]["cost_yuan"] is None
    assert len(created) == 2 and len(closed) == 2
    session.close()
    assert len(closed) == 2


def test_simultaneous_identical_requests_share_one_provider_call(monkeypatch, tmp_path):
    calls = []
    def http(url, timeout):
        calls.append(timeout)
        time.sleep(0.06)
        return {"item": detail("1")}
    monkeypatch.setattr("data_sources._http_get_json", http)
    client = OneboundClient(EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}, runtime={"detail_cache_seconds": 0}), tmp_path)
    try:
        results = parallel_map(["1"] * 4, client.item_get, concurrency=4, timeout=1)
        assert all(row["ok"] for row in results)
        assert len(calls) == 1
        assert client.stats.to_dict()["coalesced_hits"] == 3
        results[0]["value"]["title"] = "changed"
        assert "title" not in results[1]["value"]
    finally:
        client.close()


def test_provider_retry_cannot_outlive_remaining_budget(monkeypatch, tmp_path):
    calls = []
    def http(url, timeout):
        calls.append(timeout)
        raise OSError("transient fixture")
    monkeypatch.setattr("data_sources._http_get_json", http)
    client = OneboundClient(EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}), tmp_path)
    try:
        with cancellation_scope(0.06):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                client.item_get("1")
            assert time.monotonic() - started < 0.3
        assert len(calls) == 1 and 0 < calls[0] <= 0.060001
        assert not list(tmp_path.glob("*.json"))
    finally:
        client.close()


def test_late_provider_response_is_not_written_to_cache(monkeypatch, tmp_path):
    release, finished = threading.Event(), threading.Event()
    def http(url, timeout):
        release.wait(1)
        return {"item": detail("1")}
    monkeypatch.setattr("data_sources._http_get_json", http)
    client = OneboundClient(EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}), tmp_path)
    def work(_):
        try:
            return client.item_get("1")
        finally:
            finished.set()
    try:
        result = parallel_map([1], work, timeout=0.04)
        assert result[0]["ok"] is False
        release.set()
        assert finished.wait(1)
        assert not list(tmp_path.glob("*.json"))
    finally:
        release.set()
        client.close()


def test_refresh_bypass_does_not_disable_later_cache(monkeypatch, tmp_path):
    calls = []
    def http(url, timeout):
        calls.append(1)
        return {"item": detail("1")}
    monkeypatch.setattr("data_sources._http_get_json", http)
    cfg = EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}, runtime={"detail_cache_seconds": 60})
    client = OneboundClient(cfg, tmp_path)
    try:
        client.item_get("1")
        token = refresh_details.set(True)
        try:
            client.item_get("1")
        finally:
            refresh_details.reset(token)
        client.item_get("1")
        assert len(calls) == 2
        assert cfg.runtime["detail_cache_seconds"] == 60
    finally:
        client.close()


def test_only_final_six_and_two_backups_need_refresh():
    candidates = []
    for i in range(20):
        item = offer(str(i))
        item["detail"] = detail(str(i))
        item["detail"]["fetched_at"] -= 120
        item["detail"]["skus"]["sku"][0]["price"] = 10 + i
        candidates.append(item)
    selected = select_refresh_candidates({"target": wanted(), "candidates": list(reversed(candidates))})
    assert len(selected) == 8
    assert {item["num_iid"] for item in selected} == {str(i) for i in range(8)}
    assert [item["num_iid"] for item in selected] == [str(i) for i in range(8)]


def test_provider_fees_are_unknown_without_explicit_configuration(tmp_path):
    client = OneboundClient(EcomConfig("onebound", {"key": "fixture", "secret": "fixture"}, {}), tmp_path)
    try:
        client.stats.record("item_get")
        assert client.stats.to_dict()["cost_yuan"] is None
        assert client.stats.to_dict()["actual_cost_yuan"] is None
    finally:
        client.close()
    configured = DataSourceStats(0.02)
    configured.record("item_get")
    assert configured.to_dict()["estimated_cost_yuan"] == 0.02
    assert configured.to_dict()["actual_cost_yuan"] is None


def test_commercial_and_barcode_evidence_survives_normalization():
    fields = {"sales_unit": "箱", "stock_unit": "箱", "moq_unit": "箱", "currency": "CNY", "price_tax_included": False, "tax_quote": {"confirmed": True, "quantity": 3, "amount": 5, "currency": "CNY", "sku_id": "1"}, "shipping_quote": {"confirmed": True, "quantity": 3, "destination": {"province": "浙江省"}, "scope": "sku", "sku_id": "1", "amount": 8}, "discounts": [{"confirmed": True, "regions": ["浙江省"], "free_threshold": 100}], "barcode": "1234567890123", "barcode_scope": "case", "identifiers": [{"type": "gtin", "scope": "unknown", "value": "1234567890123"}], "deliverable": True}
    result = _normalize_mcp_detail_response({"result": {**fields, "productSkuInfos": [{**fields, "skuId": "1", "price": 12, "amountOnSale": 99, "priceRanges": [{"beginQuantity": 1, "endQuantity": 9, "price": 12}]}]}})
    row = result["skus"]["sku"][0]
    assert all(row[key] == value and result[key] == value for key, value in fields.items())
    assert row["price_tiers"][0]["endQuantity"] == 9


def test_refresh_preserves_image_barcode_without_overwriting_api_evidence():
    api = {"value": "4006381333931", "level": "unit", "source": "sku.barcode"}
    image = {"value": "4006381333931", "level": "unit", "source": "image:https://img.example/label.jpg", "evidence": "商品条码4006381333931"}
    original = detail("1")
    row = original["skus"]["sku"][0]
    row.update(barcode="4006381333931", barcode_level="unit", identifiers=[api, image], vision={"status": "matched"})
    item = {**offer("1"), "detail": original}
    fresh = copy.deepcopy(original)
    new_row = fresh["skus"]["sku"][0]
    new_row.pop("vision")
    new_row["identifiers"] = [api]
    _merge_confirmed_detail(item, fresh)
    merged_row = item["detail"]["skus"]["sku"][0]
    assert merged_row["vision"]["status"] == "matched"
    assert merged_row["identifiers"] == [api, image]
    changed = copy.deepcopy(fresh)
    changed_row = changed["skus"]["sku"][0]
    changed_row.pop("vision", None)
    changed_row.update(barcode="5901234123457", identifiers=[{**api, "value": "5901234123457"}])
    _merge_confirmed_detail(item, changed)
    assert "vision" not in item["detail"]["skus"]["sku"][0]
    assert item["detail"]["skus"]["sku"][0]["identifiers"] == [{**api, "value": "5901234123457"}]
