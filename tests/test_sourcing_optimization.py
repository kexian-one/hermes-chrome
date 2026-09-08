from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from product_match import parse_attributes, select_sku
from sourcing_rules import run_pipeline, spec_in_text
from keyword_builder import build_keywords
from runtime_support import cache_key, read_cache, write_cache, parallel_map
from data_sources import _normalize_mcp_detail_response
from agent.artifacts import validate_sourcing_output
from agent.llm_client import ChatResponse, non_thinking_body
from agent.product_vision import ProductVision
from agent.worker_state import WorkerStateTracker


def target() -> dict:
    return {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干", "selected_sku": "草莓100g×6袋", "buy_multiple": 3, "jd_price": 30}


def offer(*names: str) -> dict:
    return {"num_iid": "123", "title": "样例牌饼干多规格", "unitPrice": 1, "compositeScore": 5, "shopYear": 5, "detail": {"skus": {"sku": [{"sku_id": str(i), "properties_name": name, "price": 12 + i, "quantity": 100, "sku_image_url": f"https://img.example/{i}.jpg"} for i, name in enumerate(names)]}}}


@pytest.mark.parametrize("flavor", ["草莓", "巧克力", "香草", "柠檬", "蓝莓"])
@pytest.mark.parametrize("grams", [50, 100, 200, 500])
@pytest.mark.parametrize("count", [1, 3, 6, 12, 24])
def test_same_brand_near_negative_matrix(flavor: str, grams: int, count: int) -> None:
    wanted = target()
    wanted["selected_sku"] = f"{flavor}{grams}g×{count}袋"
    candidate = offer(f"{flavor}{grams}g×{count}袋")
    selected = select_sku(candidate, wanted)
    assert selected["match"]["status"] == "matched"
    wrong_flavor = "巧克力" if flavor != "巧克力" else "草莓"
    for wrong in (f"{wrong_flavor}{grams}g×{count}袋", f"{flavor}{grams+1}g×{count}袋", f"{flavor}{grams}g×{count+1}袋", f"{flavor}{grams}g×{count}瓶"):
        assert not run_pipeline({"target": wanted, "candidates": [offer(wrong)]})["final"]


def test_selected_sku_price_stock_and_image_are_bound() -> None:
    candidate = offer("巧克力100g×6袋", "草莓100g×6袋")
    result = run_pipeline({"target": target(), "candidates": [candidate]})["final"][0]
    assert result["selected_sku"]["sku_id"] == "1"
    assert result["unitPrice"] == 13
    assert result["selected_sku"]["sku_image_url"].endswith("/1.jpg")
    assert result["selected_sku"]["purchase_total"] == 39
    candidate["detail"]["skus"]["sku"][1]["quantity"] = 0
    assert not run_pipeline({"target": target(), "candidates": [candidate]})["final"]


def test_attributes_cannot_be_combined_across_rows() -> None:
    assert not run_pipeline({"target": target(), "candidates": [offer("草莓50g×6袋", "巧克力100g×6袋")]})["final"]


def test_precomputed_match_label_does_not_bypass_validation() -> None:
    candidate = {**offer("巧克力100g×6袋"), "skuMatchLevel": "完全一致"}
    assert not run_pipeline({"target": target(), "candidates": [candidate]})["final"]


def test_unit_conversion_and_nested_packaging() -> None:
    assert spec_in_text("1kg", "净含量1000g")
    assert spec_in_text("0.5L", "500毫升")
    assert not spec_in_text("500g", "500ml")
    assert not spec_in_text("75g", "750g")
    attrs = parse_attributes("草莓100g×6袋×2盒")
    assert attrs["pack_count"] == 12
    assert attrs["pack_structure"] == "6袋*2盒"


def test_unknown_stock_stays_unknown() -> None:
    detail = _normalize_mcp_detail_response({"result": {"offerId": "1", "productSkuInfos": [{"skuId": "s", "price": 3}]}})
    assert detail["num"] is None
    assert detail["skus"]["sku"][0]["quantity"] is None


def test_keyword_brand_is_not_color_and_brackets_keep_spec() -> None:
    assert "白" not in build_keywords("白猫柠檬洗洁精500g").variant
    assert build_keywords("红鸟黑色鞋油75g").variant == ["黑色"]
    assert build_keywords("可口可乐原味汽水330ml×24罐").brand == "可口可乐"
    assert build_keywords("陌生品牌饼干【100g×6袋】").spec == "100g"
    assert build_keywords("陌生品牌饼干").brand is None


def test_tier_price_and_inventory_for_purchase_quantity() -> None:
    item = offer("草莓100g×6袋")
    row = item["detail"]["skus"]["sku"][0]
    row["price_tiers"] = [{"startQuantity": 1, "price": 12}, {"startQuantity": 10, "price": 9}]
    wanted = {**target(), "buy_multiple": 10}
    selected = select_sku(item, wanted)
    assert selected["sku_price"] == 9
    assert selected["purchase_total"] == 90
    row["quantity"] = 9
    assert not run_pipeline({"target": wanted, "candidates": [item]})["final"]


def test_pack_substitution_requires_explicit_option_and_rounded_purchase() -> None:
    item = offer("草莓100g×4袋")
    assert not run_pipeline({"target": target(), "candidates": [item]})["final"]
    selected = select_sku(item, {**target(), "allow_pack_substitution": True})
    assert selected["order_quantity"] == 5
    assert selected["purchase_total"] == 60
    assert selected["unit_price"] == 20


def test_cache_expiry_and_long_url_collision(tmp_path: Path) -> None:
    assert cache_key("img", "x" * 240 + "a") != cache_key("img", "x" * 240 + "b")
    path = tmp_path / "cache.json"
    write_cache(path, {"stock": 3})
    assert read_cache(path, 60) == {"stock": 3}
    assert read_cache(path, 0) is None
    path.write_text("{broken", encoding="utf-8")
    assert read_cache(path, 60) is None


def test_single_item_failure_isolated_and_concurrency_bounded() -> None:
    import threading
    active = peak = 0
    lock = threading.Lock()
    def task(item: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.01)
            if item == 2:
                raise RuntimeError("provider failure")
            return item
        finally:
            with lock:
                active -= 1
    results = parallel_map(list(range(10)), task, concurrency=3, timeout=2)
    assert sum(r["ok"] for r in results) == 9
    assert 1 < peak <= 3


def test_worker_restart_state_preserves_task(tmp_path: Path) -> None:
    state = WorkerStateTracker(tmp_path / "state.json")
    state.update_spawn("b1", "ecom-best-source", 9, "草莓100g×6袋 https://item.jd.com/123.html", "/outputs/task")
    recovered = WorkerStateTracker(tmp_path / "state.json").snapshot()[0]
    assert recovered.last_task.startswith("草莓100g×6袋")
    assert recovered.output_dir == "/outputs/task"
    assert not recovered.alive


def test_csv_manifest_detects_empty_and_modified_files(tmp_path: Path) -> None:
    from sourcing_pipeline import run_from_files
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"target": target(), "candidates": [offer("草莓100g×6袋")]}), encoding="utf-8")
    csv_path = tmp_path / "result.csv"
    summary = run_from_files(jd_product_path=None, candidates_path=None, merged_input_path=str(path), output_path=str(csv_path), confirm_details=False)
    assert summary["final_count"] == 1
    assert validate_sourcing_output(csv_path)
    csv_path.write_text("", encoding="utf-8")
    assert validate_sourcing_output(csv_path) is None


@pytest.mark.asyncio
async def test_vision_mismatch_veto_and_unknown_on_failure(tmp_path: Path) -> None:
    llm = AsyncMock()
    llm._model = "qwen3.8-flash"
    llm.structured.return_value = ChatResponse(text=json.dumps({"attributes": {"flavor": "巧克力"}, "evidence": {"flavor": "巧克力味"}, "status": "mismatch", "differences": []}), tool_calls=[], finish_reason="stop")
    wanted = {**target(), "main_image_url": "https://img.example/target.jpg", "require_vision": True}
    candidate = offer("草莓100g×6袋")
    vision = ProductVision(llm, tmp_path, image_loader=AsyncMock(side_effect=lambda url: ("data:image/png;base64,AAAA", url)))
    await vision.verify_candidates(wanted, [candidate])
    assert not run_pipeline({"target": wanted, "candidates": [candidate]})["final"]
    llm.structured.side_effect = TimeoutError()
    candidate = offer("草莓100g×6袋")
    candidate["num_iid"] = "456"
    await vision.verify_candidates(wanted, [candidate])
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "unknown"


def test_qwen_thinking_parameter() -> None:
    assert non_thinking_body("qwen3.8-flash", {"thinking": {"type": "disabled"}}) == {"enable_thinking": False}


def test_unknown_target_packaging_cannot_approve_arbitrary_candidate() -> None:
    wanted = {**target(), "unconfirmed_attributes": ["pack_count", "packaging"]}
    result = run_pipeline({"target": wanted, "candidates": [offer("草莓100g×6袋")]})
    assert not result["final"] and len(result["pending"]) == 1
    assert "target.pack_count" in result["pending"][0]["match_evidence"]["missing"]
