from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.llm_client import ChatResponse
from agent.product_vision import ProductVision
from product_identifiers import validate_gtin, normalize_identifiers, compare_identifiers


ATTRS = {"brand": "样例牌", "category": "饼干", "flavor": "草莓", "net_content": "100g", "pack_count": 6, "packaging": "袋装"}


def facts(attrs=None, **changes):
    attrs = dict(ATTRS) if attrs is None else attrs
    return {"product_visible": True, "readability": "clear", "attributes": attrs, "evidence": {key: str(value) for key, value in attrs.items()}, "identifiers": [], **changes}


class FactsLLM:
    _model = "offline-facts"

    def __init__(self, payload):
        self.payload, self.calls = payload, []

    async def structured(self, prompt, data, images):
        self.calls.append({"prompt": prompt, "data": copy.deepcopy(data), "images": list(images)})
        if data.get("operation") == "read_product_label":
            result = self.payload(data) if callable(self.payload) else self.payload
        else:
            result = {"attributes": {}, "evidence": {}, "status": "unknown", "differences": [], "reason": "离线对比无法补充图片证据"}
        return ChatResponse(text=json.dumps(result, ensure_ascii=False), tool_calls=[], finish_reason="stop")


def target(**changes):
    return {"brand": "样例牌", "category": "饼干", "attributes": dict(ATTRS), "main_image_url": "https://img.alicdn.com/target.jpg", "require_vision": True, **changes}


def offer(offer_id="one", name="草莓100g×6袋", image="https://img.alicdn.com/shared.jpg", **changes):
    return {"num_iid": offer_id, "title": "样例牌饼干", "detail": {"skus": {"sku": [{"sku_id": offer_id, "properties_name": name, "price": 10, "quantity": 100, "sku_image_url": image, **changes}]}}}


def mocked_processor(tmp_path, payload):
    llm = FactsLLM(payload)
    async def load_image(url):
        content = url.encode()
        return "data:image/png;base64," + base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest()
    loader = AsyncMock(side_effect=load_image)
    return ProductVision(llm, tmp_path, image_loader=loader), llm, loader


@pytest.mark.parametrize("value,expected", [("4006381333931", "4006381333931"), ("036000291452", "036000291452"), ("0036000291452", "0036000291452"), ("04006381333931", "04006381333931"), ("96385074", "96385074"), ("４００６３８１３３３９３１", "4006381333931"), ("400-6381 333931", "4006381333931"), ("4006381333932", None), ("400638133393", None), ("0000000000000", None), ("O4006381333931", None), (-4006381333931, None), (True, None), (None, None)])
def test_gtin_formats_and_check_digits(value, expected):
    assert validate_gtin(value) == expected


def test_identifier_scope_is_explicit_and_never_inferred_from_length():
    result = normalize_identifiers([{"value": "04006381333931"}, {"value": "4006381333931", "level": "case"}, {"value": "4006381333932", "level": "unit"}])
    assert [entry["level"] for entry in result] == ["unknown", "case"]
    assert all(set(entry) == {"value", "level", "evidence", "source"} for entry in result)


def test_image_barcode_requires_matching_visible_digits_in_its_evidence():
    entries = [{"value": "4006381333931", "level": "unit", "evidence": "5901234123457"}, {"value": "4006381333931", "level": "unit"}]
    assert normalize_identifiers(entries, source="image:https://img.alicdn.com/label.jpg") == []
    assert normalize_identifiers([{**entries[0], "evidence": "单件条码４００６３８１３３３９３１"}], source="image:fixture")


def test_upc_and_zero_padded_ean_compare_at_the_same_level():
    result = compare_identifiers([{"value": "036000291452", "level": "unit"}], [{"value": "0036000291452", "level": "unit"}])
    assert result["status"] == "matched" and not result["conflicts"]


@pytest.mark.parametrize("candidate", [[], [{"value": "5901234123457", "level": "case"}], [{"value": "5901234123457", "level": "unknown"}], [{"value": "5901234123458", "level": "unit"}]])
def test_missing_unknown_and_other_level_identifiers_do_not_conflict(candidate):
    assert compare_identifiers([{"value": "4006381333931", "level": "unit"}], candidate)["status"] == "unknown"


def test_same_level_conflict_and_ambiguous_multiple_codes_are_distinct():
    wanted = [{"value": "4006381333931", "level": "unit"}]
    other = [{"value": "5901234123457", "level": "unit"}]
    assert compare_identifiers(wanted, other)["status"] == "conflict"
    result = compare_identifiers(wanted, wanted + other)
    assert result["status"] == "unknown" and result["ambiguous_levels"] == ["unit"]


@pytest.mark.asyncio
async def test_same_image_facts_are_reused_without_reusing_a_sku_verdict(tmp_path):
    processor, llm, loader = mocked_processor(tmp_path, facts())
    first, second = offer(), offer("two", name="草莓100g×12袋")
    wanted = target(allow_pack_substitution=True)
    await processor.verify_candidates(wanted, [first, second])
    assert first["detail"]["skus"]["sku"][0]["vision"]["status"] == "matched"
    assert second["detail"]["skus"]["sku"][0]["vision"]["status"] == "mismatch"
    assert len(llm.calls) == loader.await_count == 1
    assert set(llm.calls[0]["data"]) == {"operation", "image_sha256"}
    assert processor.metrics["verified_skus"] == processor.metrics["deterministic_comparisons"] == 2
    await processor.aclose()


@pytest.mark.asyncio
async def test_same_bytes_at_different_urls_share_only_independent_facts(tmp_path):
    llm = FactsLLM(facts())
    loader = AsyncMock(return_value=("data:image/png;base64,AAAA", "identical-content-digest"))
    processor = ProductVision(llm, tmp_path, image_loader=loader)
    first, second = await asyncio.gather(processor._image_facts("https://img.alicdn.com/a.jpg"), processor._image_facts("https://img.alicdn.com/b.jpg"))
    assert len(llm.calls) == 1 and loader.await_count == 2
    assert first["image_url"] != second["image_url"]
    first["attributes"]["flavor"] = "污染测试"
    assert second["attributes"]["flavor"] == "草莓"
    await processor.aclose()


@pytest.mark.asyncio
async def test_http_connection_and_disk_image_cache_are_reused(tmp_path, monkeypatch):
    clients, downloads = [], []
    real_client = httpx.AsyncClient
    def request(request):
        downloads.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"fixture-image:" + request.url.path.encode())
    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(request), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr("agent.product_vision.httpx.AsyncClient", factory)
    first = ProductVision(FactsLLM(facts()), tmp_path)
    await asyncio.gather(first._image_facts("https://img.alicdn.com/a.jpg"), first._image_facts("https://img.alicdn.com/b.jpg"))
    assert len(clients) == 1 and len(downloads) == 2
    await first.aclose()
    assert clients[0].is_closed
    second_llm = FactsLLM(facts())
    second = ProductVision(second_llm, tmp_path)
    await second._image_facts("https://img.alicdn.com/a.jpg")
    assert len(downloads) == 2 and not second_llm.calls
    assert second.metrics["image_cache_hits"] >= 1 and second.metrics["ocr_cache_hits"] == 1
    await second.aclose()


@pytest.mark.asyncio
async def test_expired_url_cache_and_corrupt_bytes_require_redownload(tmp_path, monkeypatch):
    downloads = []
    async def download(url):
        downloads.append(url)
        content = b"correct-image"
        return "data:image/png;base64," + base64.b64encode(content).decode(), hashlib.sha256(content).hexdigest()
    first = ProductVision(FactsLLM(facts()), tmp_path)
    monkeypatch.setattr(first, "_download_image", download)
    await first._image_facts("https://img.alicdn.com/a.jpg")
    next((tmp_path / "images").glob("*.bin")).write_bytes(b"corrupt-cache")
    second = ProductVision(FactsLLM(facts()), tmp_path)
    monkeypatch.setattr(second, "_download_image", download)
    await second._image_facts("https://img.alicdn.com/a.jpg")
    third = ProductVision(FactsLLM(facts()), tmp_path, image_cache_seconds=0)
    monkeypatch.setattr(third, "_download_image", download)
    await third._image_facts("https://img.alicdn.com/a.jpg")
    assert len(downloads) == 3
    await asyncio.gather(first.aclose(), second.aclose(), third.aclose())


@pytest.mark.asyncio
async def test_unreadable_picture_cannot_borrow_text_or_a_valid_barcode_to_match(tmp_path):
    code = {"value": "4006381333931", "level": "unit", "readable": True, "evidence": "4006381333931"}
    processor, llm, _ = mocked_processor(tmp_path, facts({}, product_visible=False, readability="unreadable", identifiers=[code]))
    candidate = offer()
    await processor.verify_candidates(target(identifiers=[code]), [candidate])
    row = candidate["detail"]["skus"]["sku"][0]
    assert row["vision"]["status"] == "unknown"
    assert row["vision"]["identifier_comparison"]["status"] == "matched"
    assert len(llm.calls) == 1
    await processor.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("level,value,expected", [("unit", "5901234123457", "unknown"), ("case", "5901234123457", "matched"), ("unknown", "5901234123457", "matched"), ("unit", "4006381333932", "matched")])
async def test_barcode_is_auxiliary_and_scoped_to_packaging_level(tmp_path, level, value, expected):
    wanted = target(identifiers=[{"value": "4006381333931", "level": "unit"}])
    processor, _, _ = mocked_processor(tmp_path, facts(identifiers=[{"value": value, "level": level, "readable": True, "evidence": value}]))
    candidate = offer()
    await processor.verify_candidates(wanted, [candidate])
    row = candidate["detail"]["skus"]["sku"][0]
    assert row["vision"]["status"] == expected
    if value == "4006381333932":
        assert row["identifiers"] == []
    await processor.aclose()


@pytest.mark.asyncio
async def test_explicit_sku_label_image_adds_barcode_without_mixing_unbound_images(tmp_path):
    label_url = "https://img.alicdn.com/label.jpg"
    label_hash = hashlib.sha256(label_url.encode()).hexdigest()
    code = {"value": "4006381333931", "level": "unit", "readable": True, "evidence": "单件条码4006381333931"}
    processor, llm, loader = mocked_processor(tmp_path, lambda data: facts(identifiers=[code] if data["image_sha256"] == label_hash else []))
    candidate = offer(label_image_urls=[label_url])
    await processor.verify_candidates(target(), [candidate])
    row = candidate["detail"]["skus"]["sku"][0]
    assert row["identifiers"][0] == {"value": "4006381333931", "level": "unit", "evidence": code["evidence"], "source": "image:" + label_url}
    assert row["vision"]["status"] == "matched"
    assert len(llm.calls) == loader.await_count == 2
    await processor.aclose()
