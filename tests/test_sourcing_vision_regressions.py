from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.llm_client import ChatResponse
from agent.product_vision import ProductVision
from product_match import select_sku


ATTRS = {"brand": "样例牌", "category": "饼干", "flavor": "草莓", "net_content": "100g", "pack_count": 6, "packaging": "袋装"}


def target(**changes):
    return {"brand": "样例牌", "category": "饼干", "attributes": dict(ATTRS), "main_image_url": "https://offline.example/target.jpg", "require_vision": True, **changes}


def sku(name="草莓100g×6袋", sku_id="one", **changes):
    return {"sku_id": sku_id, "properties_name": name, "price": 10, "quantity": 100, "sku_image_url": f"https://offline.example/{sku_id}.jpg", **changes}


def offer(offer_id="offer", rows=None, **changes):
    return {"num_iid": offer_id, "title": "样例牌饼干", "shopName": offer_id, "detail": {"skus": {"sku": rows if rows is not None else [sku()]}}, **changes}


def reply(attrs=None, status="matched", **changes):
    attrs = dict(ATTRS) if attrs is None else attrs
    return {"attributes": attrs, "evidence": {k: str(v) for k, v in attrs.items()}, "status": status, "differences": [], **changes}


class FakeLLM:
    _model = "offline-fixture"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.fact_calls = []

    async def structured(self, prompt, data, images):
        if data.get("operation") == "read_product_label":
            self.fact_calls.append({"data": copy.deepcopy(data), "images": list(images)})
            return ChatResponse(text=json.dumps({"attributes": {}, "evidence": {}, "identifiers": []}), tool_calls=[], finish_reason="stop")
        self.calls.append({"data": copy.deepcopy(data), "images": list(images)})
        result = self.responses(data) if callable(self.responses) else self.responses.pop(0) if isinstance(self.responses, list) else self.responses
        if isinstance(result, Exception):
            raise result
        return ChatResponse(text=json.dumps(result, ensure_ascii=False), tool_calls=[], finish_reason="stop")


def vision(tmp_path: Path, responses):
    llm = FakeLLM(responses)
    loader = AsyncMock(side_effect=lambda url: ("data:image/png;base64,AAAA", url))
    return ProductVision(llm, tmp_path, image_loader=loader), llm, loader


@pytest.mark.asyncio
async def test_sales_pack_text_and_partial_label_image_can_jointly_prove_sku(tmp_path):
    processor, llm, _ = vision(tmp_path, reply({k: v for k, v in ATTRS.items() if k not in {"category", "pack_count"}}))
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    selected = select_sku(candidate, target())
    assert selected["match"]["status"] == "matched"
    assert selected["vision"]["positive_evidence"] is True
    assert llm.calls[0]["data"]["candidate"]["pack_count"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [reply({}, "matched"), reply(ATTRS, "unknown"), reply({"brand": "样例牌"}, "matched"), reply({"packaging": "袋装"}, "matched")])
async def test_text_match_requires_positive_image_evidence(tmp_path, payload):
    processor, _, _ = vision(tmp_path, payload)
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_actual_label_conflict_vetoes_model_matched_status(tmp_path):
    processor, llm, _ = vision(tmp_path, reply({**ATTRS, "flavor": "巧克力"}))
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    assert select_sku(candidate, target())["match"]["status"] == "mismatch"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_contradictory_model_response_retries_once_and_never_silently_passes(tmp_path, recover):
    contradictory = reply(differences=[{"field": "flavor", "target": "草莓", "candidate": "巧克力"}])
    processor, llm, _ = vision(tmp_path, [contradictory, reply() if recover else contradictory])
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    result = candidate["detail"]["skus"]["sku"][0]["vision"]
    assert result["status"] == ("matched" if recover else "unknown")
    assert len(llm.calls) == 2
    assert processor.metrics["response_retries"] == 1


@pytest.mark.asyncio
async def test_custom_brand_alias_is_consistent_in_text_image_comparison(tmp_path):
    wanted = target(brand_aliases=["ExampleCo"])
    candidate = offer(title="ExampleCo 饼干")
    processor, _, _ = vision(tmp_path, reply())
    await processor.verify_candidates(wanted, [candidate])
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "matched"


@pytest.mark.asyncio
async def test_conflicting_text_sources_cannot_be_washed_clean_by_image(tmp_path):
    candidate = offer(rows=[sku(attributes={"flavor": "巧克力"})])
    processor, llm, _ = vision(tmp_path, reply())
    result = await processor.verify_candidates(target(), [candidate])
    evidence = candidate["detail"]["skus"]["sku"][0]["vision"]
    assert result["attempted"] == 0 and not llm.calls
    assert evidence["status"] == "unknown" and evidence["code"] == "text_conflict"
    assert evidence["attribute_conflicts"][0]["field"] == "flavor"


@pytest.mark.asyncio
async def test_supplier_with_many_unknown_skus_cannot_consume_entire_budget(tmp_path):
    many = offer("many", [sku("100g", str(i)) for i in range(24)])
    exact = offer("exact")
    processor, llm, _ = vision(tmp_path, lambda data: reply() if data["offer_id"] == "exact" else reply({}, "unknown"))
    result = await processor.verify_candidates(target(), [many, exact], 2)
    assert result == {"attempted": 2, "remaining": 23}
    assert {call["data"]["offer_id"] for call in llm.calls} == {"many", "exact"}
    assert exact["detail"]["skus"]["sku"][0]["vision"]["status"] == "matched"
    deferred = many["detail"]["skus"]["sku"][1]["vision"]
    assert deferred["code"] == "budget_deferred"
    assert "图片" in deferred["reason"] and "缺少" not in deferred["reason"]


@pytest.mark.asyncio
async def test_offers_from_same_supplier_share_first_round_quota(tmp_path):
    first = offer("a", shopName="同一供应商")
    second = offer("b", shopName="同一供应商")
    other = offer("c", shopName="另一供应商")
    processor, llm, _ = vision(tmp_path, reply())
    await processor.verify_candidates(target(), [first, second, other], 2)
    assert {call["data"]["offer_id"] for call in llm.calls} == {"a", "c"}


@pytest.mark.asyncio
async def test_supplier_member_id_unifies_different_offer_shop_titles(tmp_path):
    first, second, other = offer("a", memberId="same"), offer("b", memberId="same"), offer("c", memberId="different")
    processor, llm, _ = vision(tmp_path, reply())
    await processor.verify_candidates(target(), [first, second, other], 2)
    assert {call["data"]["offer_id"] for call in llm.calls} == {"a", "c"}


@pytest.mark.asyncio
async def test_legacy_refill_packaging_is_routed_to_separate_attribute(tmp_path):
    wanted = target(attributes={**ATTRS, "packaging_variant": "补充装"})
    candidate = offer(rows=[sku("草莓100g×6袋补充装")])
    processor, _, _ = vision(tmp_path, reply({**ATTRS, "packaging": "补充装"}))
    await processor.verify_candidates(wanted, [candidate])
    row = candidate["detail"]["skus"]["sku"][0]
    assert row["vision"]["status"] == "matched"
    assert row["vision_attributes"]["packaging_variant"] == "补充装"
    assert "packaging" not in row["vision_attributes"]
    assert row["vision"]["evidence"]["packaging_variant"] == "补充装"


@pytest.mark.asyncio
async def test_conflicting_legacy_and_new_packaging_fields_are_not_overwritten(tmp_path):
    processor, llm, _ = vision(tmp_path, reply({**ATTRS, "packaging": "补充装", "packaging_variant": "正装"}))
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "unknown"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_zero_budget_does_not_force_one_model_call(tmp_path):
    candidate = offer()
    processor, llm, _ = vision(tmp_path, reply())
    assert await processor.verify_candidates(target(), [candidate], 0) == {"attempted": 0, "remaining": 1}
    assert not llm.calls
    assert "attempt_count" not in candidate["detail"]["skus"]["sku"][0]["vision"]


@pytest.mark.asyncio
async def test_repeated_round_skips_completed_unknown_but_can_use_new_bound_image(tmp_path):
    candidate = offer()
    processor, llm, _ = vision(tmp_path, [reply({}, "unknown"), reply()])
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 1
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 0
    candidate["detail"]["skus"]["sku"][0]["sku_image_urls"] = ["https://offline.example/back.jpg"]
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 1
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_model_errors_are_limited_to_two_rounds_per_unchanged_sku(tmp_path):
    processor, llm, _ = vision(tmp_path, lambda _: TimeoutError())
    candidate = offer()
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 1
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 1
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 0
    assert len(llm.calls) == 2
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_budget_deferral_does_not_reset_failed_sku_attempt_counter(tmp_path):
    processor, llm, _ = vision(tmp_path, lambda _: TimeoutError())
    candidate = offer()
    await processor.verify_candidates(target(), [candidate])
    await processor.verify_candidates(target(), [candidate], 0)
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["attempt_count"] == 1
    await processor.verify_candidates(target(), [candidate])
    assert (await processor.verify_candidates(target(), [candidate]))["attempted"] == 0
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_supplemental_bound_label_image_fills_missing_attribute(tmp_path):
    candidate = offer(rows=[sku("100g×6袋", sku_image_urls=["https://offline.example/back.jpg"])])
    front = reply({"brand": "样例牌", "net_content": "100g"}, "unknown")
    processor, llm, loader = vision(tmp_path, [front, reply({"flavor": "草莓", "net_content": "100g"})])
    await processor.verify_candidates(target(), [candidate])
    result = candidate["detail"]["skus"]["sku"][0]["vision"]
    assert result["status"] == "matched"
    assert len(llm.calls) == 2
    assert llm.calls[1]["data"]["focus_fields"] == ["flavor"]
    assert llm.calls[1]["data"]["image_roles"] == ["target", "candidate", "candidate"]
    assert loader.await_count == 3
    assert processor.metrics["verified_skus"] == 1


@pytest.mark.asyncio
async def test_supplement_preserves_complementary_candidate_image_fields(tmp_path):
    candidate = offer(rows=[sku("100g", sku_image_urls=["https://offline.example/back.jpg"])])
    processor, llm, _ = vision(tmp_path, [reply({"flavor": "草莓"}, "unknown"), reply({"pack_count": 6, "packaging": "袋装"})])
    await processor.verify_candidates(target(), [candidate])
    row = candidate["detail"]["skus"]["sku"][0]
    assert row["vision"]["status"] == "matched"
    assert row["vision_attributes"]["flavor"] == "草莓"
    assert row["vision_attributes"]["pack_count"] == 6
    assert row["vision"]["evidence"]["flavor"] == "草莓"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_supplement_cannot_erase_conflicting_candidate_image_attributes(tmp_path):
    candidate = offer(rows=[sku("100g", sku_image_urls=["https://offline.example/back.jpg"])])
    processor, _, _ = vision(tmp_path, [reply({"flavor": "草莓"}, "unknown"), reply({"flavor": "巧克力", "pack_count": 6, "packaging": "袋装"})])
    await processor.verify_candidates(target(), [candidate])
    assert candidate["detail"]["skus"]["sku"][0]["vision"]["status"] == "mismatch"


@pytest.mark.asyncio
async def test_unbound_gallery_and_multi_sku_offer_images_never_enter_prompt(tmp_path):
    wanted = target(image_urls=["https://offline.example/unbound-target.jpg"])
    candidate = offer(rows=[sku(), sku("巧克力100g×6袋", "wrong")])
    candidate["detail"]["item_imgs"] = [{"url": "https://offline.example/other-sku.jpg"}]
    processor, llm, loader = vision(tmp_path, reply({}, "unknown"))
    await processor.verify_candidates(wanted, [candidate])
    assert len(llm.calls) == 1
    assert {call.args[0] for call in loader.await_args_list} == {wanted["main_image_url"], candidate["detail"]["skus"]["sku"][0]["sku_image_url"]}


@pytest.mark.asyncio
async def test_target_supplement_preserves_front_attributes_and_evidence(tmp_path):
    wanted = {"title": "样例牌饼干", "brand": "样例牌", "category": "饼干", "main_image_url": "https://offline.example/front.jpg", "image_urls": ["https://offline.example/back.jpg"], "image_urls_scope": "selected_sku"}
    processor, llm, _ = vision(tmp_path, [reply({"flavor": "草莓", "net_content": "100g"}), reply({"pack_count": 6, "packaging": "袋装"})])
    result = await processor.extract_target(wanted)
    assert result["attributes"]["flavor"] == "草莓"
    assert result["attributes"]["pack_count"] == 6
    assert result["vision"]["evidence"]["flavor"] == "草莓"
    assert len(result["vision"]["evidence_by_call"]) == len(llm.calls) == 2


@pytest.mark.asyncio
async def test_target_supplement_conflict_is_preserved(tmp_path):
    wanted = {"title": "样例牌饼干", "brand": "样例牌", "category": "饼干", "main_image_url": "https://offline.example/front.jpg", "image_urls": ["https://offline.example/back.jpg"], "image_urls_scope": "selected_sku"}
    processor, _, _ = vision(tmp_path, [reply({"flavor": "草莓", "net_content": "100g"}), reply({"flavor": "巧克力", "pack_count": 6, "packaging": "袋装"})])
    result = await processor.extract_target(wanted)
    assert result["target_errors"]
    assert result["vision"]["conflicts"][0]["field"] == "flavor"


@pytest.mark.asyncio
async def test_target_first_image_conflict_cannot_be_replaced_by_supplement(tmp_path):
    wanted = target(image_urls=["https://offline.example/back.jpg"], image_urls_scope="selected_sku")
    processor, llm, _ = vision(tmp_path, reply({"flavor": "巧克力"}))
    result = await processor.extract_target(wanted)
    assert result["target_errors"]
    assert len(llm.calls) == 1
