from __future__ import annotations

import pytest

from sourcing_rules import run_pipeline


@pytest.mark.parametrize("title,brand,category", [("Coca-Cola 汽水", "可口可乐", "汽水"), ("清扬洗发露", "清扬", "洗发水"), ("工厂现货", "可口可乐", "汽水")])
def test_structured_sku_evidence_and_aliases_survive_final_filters(title: str, brand: str, category: str) -> None:
    attrs = {"brand": brand, "category": category, "net_content": "500ml", "pack_count": 1, "packaging": "瓶装"}
    target = {"brand": brand, "category": category, "attributes": attrs}
    candidate = {"num_iid": "123", "title": title, "compositeScore": 5, "shopYear": 5, "detail": {"skus": [{"sku_id": "s", "attributes": attrs, "price": 10, "quantity": 20}]}}
    assert len(run_pipeline({"target": target, "candidates": [candidate]})["final"]) == 1
    candidate["detail"]["skus"][0]["attributes"] = {**attrs, "net_content": "250ml"}
    assert not run_pipeline({"target": target, "candidates": [candidate]})["final"]


@pytest.mark.parametrize("id_key", ["memberId", "sellerId", "supplierId", "seller_id", "sid"])
def test_same_supplier_cannot_fill_six_recommendation_slots(id_key: str) -> None:
    candidates = [{"num_iid": str(index), "title": "现货", "price": 10 + index, "shopName": f"同供应商别名{index}", id_key: "supplier-one"} for index in range(6)]
    candidates.append({"num_iid": "other", "title": "现货", "price": 20, id_key: "supplier-two"})
    result = run_pipeline({"target": {}, "candidates": candidates})
    assert len(result["final"]) == 2
    assert [item["num_iid"] for item in result["final"]] == ["0", "other"]
