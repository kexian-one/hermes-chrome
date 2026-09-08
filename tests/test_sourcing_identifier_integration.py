from __future__ import annotations

import copy

import pytest

from identifier_sources import identifiers_from_data
from product_match import select_sku
from sourcing_pipeline import _csv_row
from sourcing_rules import run_pipeline


def target():
    return {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干", "buy_multiple": 3,
            "barcode": "6291041500213", "barcode_level": "unit"}


def candidate(code="6291041500213", level="unit"):
    return {"num_iid": "1", "title": "样例牌饼干", "detail": {"skus": [
        {"sku_id": "s1", "properties_name": "草莓100g×6袋", "price": 10, "quantity": 100, "barcode": code, "barcode_level": level}]}}


def test_same_layer_barcode_conflict_is_pending_without_requiring_vision():
    result = run_pipeline({"target": target(), "candidates": [candidate("4006381333931")]})
    assert not result["final"]
    assert len(result["pending"]) == 1
    selected = result["pending"][0]["selected_sku"]
    assert selected["identifier_comparison"]["status"] == "conflict"
    assert "同包装层级条码冲突待核实" in selected["match"]["missing"]
    row = _csv_row(0, result["pending"][0], target())
    assert row["条码核验"] == "冲突待核实"
    assert "6291041500213" in row["目标条码"]
    assert "4006381333931" in row["货源条码"]


@pytest.mark.parametrize("code,level", [(None, "unit"), ("6291041500214", "unit"), ("4006381333931", "case"), ("4006381333931", "unknown")])
def test_missing_invalid_or_other_layer_barcode_does_not_reject(code, level):
    selected = select_sku(candidate(code, level), target())
    assert selected["match"]["status"] == "matched"
    assert selected["identifier_comparison"]["status"] == "unknown"


def test_equal_barcode_cannot_override_wrong_flavor_or_missing_visual_evidence():
    item = candidate()
    item["detail"]["skus"][0]["properties_name"] = "巧克力100g×6袋"
    assert select_sku(item, target())["match"]["status"] == "mismatch"
    selected = select_sku(candidate(), {**target(), "require_vision": True})
    assert selected["identifier_comparison"]["status"] == "matched"
    assert selected["match"]["status"] == "unknown"


def test_multi_sku_does_not_borrow_offer_or_other_row_barcode():
    item = candidate(None)
    other = copy.deepcopy(item["detail"]["skus"][0])
    other.update(sku_id="other", properties_name="巧克力100g×6袋", barcode="4006381333931")
    item["detail"]["skus"].append(other)
    item["detail"].update(barcode="4006381333931", barcode_level="unit")
    selected = select_sku(item, target())
    assert selected["sku_id"] == "s1"
    assert selected["match"]["status"] == "matched"
    assert selected["identifiers"] == []


def test_identifier_scope_alias_is_explicit_and_not_guessed_from_gtin_length():
    assert identifiers_from_data({"gtin13": "6291041500213", "gtin_scope": "case"}, "api")[0]["level"] == "case"
    assert identifiers_from_data({"gtin13": "6291041500213"}, "api")[0]["level"] == "unknown"
    assert identifiers_from_data({"attributes": {"barcode": "6291041500213", "barcode_level": "unit"}}, "api")[0]["level"] == "unit"
