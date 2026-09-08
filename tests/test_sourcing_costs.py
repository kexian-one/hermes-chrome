from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

import pytest

from product_match import select_sku
from sourcing_pipeline import _csv_row, run_from_files
from sourcing_rules import run_pipeline


def target(**values: object) -> dict:
    return {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干", "buy_multiple": 3, "jd_price": 50, "jd_price_tax_included": True, "destination": {"province": "上海市", "city": "上海市"}, **values}


def offer(**values: object) -> dict:
    return {"num_iid": "1", "title": "样例牌草莓饼干100g×6袋", "compositeScore": 5, "shopYear": 5,
            "detail": {"price_tax_included": True, "shipping_quote": {"confirmed": True, "quantity": 3, "amount": 8, "currency": "CNY", "destination": {"province": "上海市"}},
                       "skus": [{"sku_id": "s1", "properties_name": "草莓100g×6袋", "price": 10, "quantity": 100, "minOrderQuantity": 1, "unitName": "盒", "currency": "CNY"}]}, **values}


def purchase(candidate: dict, wanted: dict | None = None) -> dict:
    return select_sku(candidate, wanted or target())["purchase"]


def test_full_order_cost_uses_merchandise_plus_confirmed_freight() -> None:
    result = purchase(offer())
    assert result["status"] == "confirmed"
    assert result["components"] == {"merchandise": 30, "shipping": 8, "tax": 0, "discount": 0}
    assert result["landed_total"] == 38
    assert result["landed_unit"] == 12.67


def test_included_tax_is_never_added_twice() -> None:
    candidate = offer()
    candidate["detail"].update(tax_amount=3.9, tax_rate=.13)
    assert purchase(candidate)["landed_total"] == 38


def test_tax_and_applied_discount_have_explicit_basis() -> None:
    candidate = offer()
    candidate["detail"].update(price_tax_included=False, tax_rate=.13, tax_basis="merchandise",
        discount_quote={"confirmed": True, "quantity": 3, "amount": 5, "applied": True, "applies_to": "merchandise", "minimum_amount": 30})
    result = purchase(candidate)
    assert result["landed_total"] == 36.25
    assert result["components"]["tax"] == 3.25
    candidate["detail"]["discount_quote"]["included_in_price"] = True
    assert purchase(candidate)["landed_total"] == 41.9


@pytest.mark.parametrize("field", ["price_tax_included", "shipping_quote"])
def test_unknown_cost_component_never_becomes_zero_or_profit(field: str) -> None:
    candidate = offer()
    del candidate["detail"][field]
    result = run_pipeline({"target": target(), "candidates": [candidate]})
    assert len(result["final"]) == 1
    item = result["final"][0]
    assert item["purchase"]["status"] == "pending"
    assert item["purchase"]["landed_total"] is None
    row = _csv_row(1, item, target())
    assert row["商品金额(元)"] == "30"
    assert row["总进货价(元)"] == row["利润率"] == ""
    assert row["可购状态"] == "可购性待确认"


@pytest.mark.parametrize("changes", [{"quantity": 1}, {"sku_id": "another"}, {"destination": {"province": "浙江省"}}, {"confirmed": False}, {"expires_at": 1}])
def test_old_or_wrong_scope_freight_quote_is_not_an_order_total(changes: dict) -> None:
    candidate = offer()
    candidate["detail"]["shipping_quote"].update(changes)
    assert purchase(candidate)["components"]["shipping"] is None


def test_same_province_different_city_does_not_pass_freight_binding() -> None:
    candidate = offer()
    candidate["detail"]["shipping_quote"]["destination"] = {"province": "江苏省", "city": "南京市"}
    wanted = target(destination={"province": "江苏省", "city": "苏州市"})
    assert purchase(candidate, wanted)["components"]["shipping"] is None


@pytest.mark.parametrize("district", ["西湖区", None])
def test_exact_district_binding_cannot_fall_back_to_province_match(district: str | None) -> None:
    candidate = offer()
    candidate["detail"]["shipping_quote"]["destination"] = {"province": "浙江省", "city": "杭州市", "district": "余杭区"}
    wanted = target(destination={"province": "浙江省", "city": "杭州市", "district": district})
    assert purchase(candidate, wanted)["components"]["shipping"] is None


def test_nationwide_marker_does_not_override_contradictory_exact_destination() -> None:
    candidate = offer()
    candidate["detail"]["shipping_quote"].update(scope="nationwide", destination={"province": "浙江省", "city": "宁波市"})
    wanted = target(destination={"province": "浙江省", "city": "杭州市"})
    assert purchase(candidate, wanted)["landed_total"] is None


def test_non_rmb_quote_is_never_silently_converted() -> None:
    candidate = offer()
    candidate["detail"]["skus"][0]["currency"] = "USD"
    result = purchase(candidate)
    assert result["currency"] == "USD"
    assert result["landed_total"] is None
    assert result["status"] == "pending"


def test_first_fee_and_marketing_free_shipping_are_not_assumed_order_quotes() -> None:
    candidate = offer()
    del candidate["detail"]["shipping_quote"]
    candidate["detail"].update(post_fee=4, freeDeliverFee=True)
    assert purchase(candidate)["components"]["shipping"] is None
    assert purchase(candidate)["landed_total"] is None


def test_region_and_threshold_shipping_rules() -> None:
    candidate = offer()
    del candidate["detail"]["shipping_quote"]
    candidate["detail"]["shipping_rules"] = {"confirmed": True, "regions": ["上海"], "free_threshold": 30, "amount": 8}
    assert purchase(candidate)["landed_total"] == 30
    candidate["detail"]["shipping_rules"]["free_threshold"] = 31
    assert purchase(candidate)["landed_total"] == 38
    assert purchase(candidate, target(destination={"province": "浙江省"}))["landed_total"] is None
    assert purchase(candidate, target(destination=None))["landed_total"] is None


def test_free_shipping_threshold_respects_discount_scope() -> None:
    candidate = offer()
    del candidate["detail"]["shipping_quote"]
    candidate["detail"]["shipping_rules"] = {"confirmed": True, "scope": "nationwide", "free_threshold": 30, "amount": 8}
    candidate["detail"]["discount_quote"] = {"confirmed": True, "quantity": 3, "amount": 5, "applied": True, "applies_to": "merchandise"}
    assert purchase(candidate)["landed_total"] is None
    candidate["detail"]["shipping_rules"]["threshold_basis"] = "after_discount"
    assert purchase(candidate)["landed_total"] == 33
    candidate["detail"]["shipping_rules"]["threshold_basis"] = "before_discount"
    assert purchase(candidate)["landed_total"] == 25


def test_tier_price_checks_both_quantity_boundaries() -> None:
    candidate = offer()
    row = candidate["detail"]["skus"][0]
    row["price_tiers"] = [{"startQuantity": 1, "endQuantity": 2, "price": 11}, {"startQuantity": 3, "endQuantity": 9, "price": 9}, {"startQuantity": 10, "price": 8}]
    assert purchase(candidate)["merchandise_total"] == 27
    assert select_sku(candidate, target(buy_multiple=10))["purchase_total"] == 80
    row["price_tiers"] = [{"startQuantity": 1, "endQuantity": 2, "price": 11}]
    selected = select_sku(candidate, target())
    assert selected["sku_price"] is None
    assert "采购量未命中有效阶梯价" in selected["purchase"]["missing"]


def test_bottle_quote_and_carton_quantity_are_not_silently_multiplied_wrong() -> None:
    candidate = offer()
    row = candidate["detail"]["skus"][0]
    row.update(properties_name="草莓100g×6袋/箱", unitName="箱", priceUnit="袋", price=2)
    selected = select_sku(candidate, target())
    assert selected["price_evidence"]["pricing_quantity"] == 18
    assert selected["purchase_total"] == 36
    assert selected["purchase"]["status"] == "pending"
    assert selected["purchase"]["landed_total"] is None
    row.update(quantity=20, minOrderQuantity=7)
    selected = select_sku(candidate, target())
    assert selected["stock"] is None and selected["moq"] is None
    assert selected["purchase"]["status"] == "pending"
    row.update(stock_unit="袋", moq_unit="袋", quantity=20, minOrderQuantity=7)
    selected = select_sku(candidate, target())
    assert selected["stock"] == 3
    assert selected["moq"] == 2
    assert selected["purchase"]["status"] == "confirmed"
    assert selected["purchase"]["landed_total"] == 44


@pytest.mark.parametrize("remove,expected", [("quantity", "SKU库存待确认"), ("minOrderQuantity", "起批数待确认"), ("unitName", "销售单位待确认")])
def test_unknown_purchase_fields_are_displayed_but_not_confirmed(remove: str, expected: str) -> None:
    candidate = offer()
    del candidate["detail"]["skus"][0][remove]
    result = run_pipeline({"target": target(), "candidates": [candidate]})
    assert len(result["final"]) == 1
    assert result["final"][0]["purchaseStatus"] == "pending"
    assert expected in result["final"][0]["warnings"]


def test_confirmed_purchase_precedes_cheaper_unknown_stock() -> None:
    confirmed = offer(num_iid="confirmed")
    cheap = copy.deepcopy(confirmed)
    cheap["num_iid"] = "cheap"
    cheap["detail"]["skus"][0]["price"] = 1
    del cheap["detail"]["skus"][0]["quantity"]
    result = run_pipeline({"target": target(), "candidates": [cheap, confirmed]})
    assert [item["num_iid"] for item in result["final"]] == ["confirmed", "cheap"]
    assert result["stats"]["confirmed_purchase_count"] == 1
    assert result["stats"]["pending_purchase_count"] == 1


def test_total_landed_cost_can_reverse_goods_only_ranking() -> None:
    cheap = offer(num_iid="high-freight")
    cheap["detail"]["skus"][0]["price"] = 9
    cheap["detail"]["shipping_quote"]["amount"] = 30
    better = offer(num_iid="low-freight")
    result = run_pipeline({"target": target(), "candidates": [cheap, better]})
    assert [item["num_iid"] for item in result["final"]] == ["low-freight", "high-freight"]


def test_unavailable_stock_moq_or_delivery_are_not_approved() -> None:
    for field, value in [("quantity", 2), ("minOrderQuantity", 4)]:
        candidate = offer()
        candidate["detail"]["skus"][0][field] = value
        assert purchase(candidate)["status"] == "unavailable"
        assert not run_pipeline({"target": target(), "candidates": [candidate]})["final"]
    candidate = offer(deliverable=False)
    assert not run_pipeline({"target": target(), "candidates": [candidate]})["final"]


def test_export_keeps_old_header_order_adds_costs_and_blank_feedback(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"target": target(), "candidates": [offer()]}), encoding="utf-8")
    output = tmp_path / "result.csv"
    result = run_from_files(jd_product_path=None, candidates_path=None, merged_input_path=str(source), output_path=str(output), confirm_details=False)
    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["总进货价(元)"] == "38"
    assert rows[0]["商品金额(元)"] == "30"
    assert rows[0]["可购状态"] == "已确认可采购"
    assert rows[0]["利润率"] == "74.67%"
    assert result["confirmed_purchase_count"] == 1
    assert Path(result["feedback_path"]).is_file()
    assert Path(result["feedback_snapshot_path"]).is_file()
