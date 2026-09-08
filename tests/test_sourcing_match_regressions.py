from __future__ import annotations

import pytest

from keyword_builder import build_keywords
from product_match import (
    compare_attributes,
    normalize_value,
    parse_attributes,
    required_attribute_keys,
    row_attribute_evidence,
    select_sku,
    target_attributes,
)


def target(title: str, brand: str = "样例牌", category: str = "饼干") -> dict:
    return {"title": title, "brand": brand, "category": category}


def offer(title: str, sku: str, **row: object) -> dict:
    return {"num_iid": "1", "title": title, "detail": {"skus": [{"sku_id": "one", "properties_name": sku, "price": 6, "quantity": 100, "minOrderQuantity": 1, **row}]}}


@pytest.mark.parametrize("text,count", [
    ("100g×6袋（单袋100g）", 6),
    ("单瓶容量500ml×24瓶", 24),
    ("单袋100g，每盒6袋，2盒装", 12),
    ("100g×6袋/盒×2盒", 12),
    ("100g×2盒×6袋", 12),
    ("100g单袋", 1),
])
def test_single_item_description_does_not_replace_sales_pack(text: str, count: int) -> None:
    assert parse_attributes(text)["pack_count"] == count


def test_carton_and_single_bottle_cannot_match() -> None:
    wanted = target("样例牌汽水单瓶500ml×24瓶", category="汽水")
    candidate = offer("样例牌汽水500ml单瓶", "500ml单瓶")
    selected = select_sku(candidate, wanted)
    assert selected["match"]["status"] == "mismatch"
    assert {difference["field"] for difference in selected["match"]["differences"]} == {"pack_count"}


def test_sales_unit_and_gifts_remain_separate_from_paid_pack() -> None:
    attrs = parse_attributes("草莓100g×6袋/箱+赠50g1袋")
    assert attrs["net_content"] == "100g"
    assert attrs["pack_count"] == 6
    assert attrs["sales_unit"] == "箱"
    assert attrs["gift_count"] == 1
    assert compare_attributes(attrs, {key: value for key, value in attrs.items() if key != "gift_count"})["status"] == "matched"
    assert parse_attributes("100g买6赠1袋")["pack_count"] == 6
    assert parse_attributes("500ml×24瓶整箱装")["sales_unit"] == "箱"
    assert normalize_value("packaging", "箱") == "箱装"
    assert "sales_unit" not in parse_attributes("100g×6袋×2盒")
    assert "gift_count" not in parse_attributes("100g×6袋+赠100g")


def test_equivalent_nested_pack_expressions_share_structure() -> None:
    left = parse_attributes("100g×6袋×2盒")
    right = parse_attributes("100g×2盒×6袋")
    assert left == right
    assert left["packaging"] == "袋装"


def test_total_weight_does_not_erase_unit_weight() -> None:
    assert parse_attributes("100g×6袋（总重600g）")["net_content"] == "100g"


def test_paper_count_is_not_net_weight_and_same_sku_can_match() -> None:
    title = "清风抽纸3层100抽24包200mm×130mm"
    wanted = build_keywords(title).to_target(title)
    expected = target_attributes(wanted)
    assert "net_content" not in expected
    assert expected["pack_count"] == 24
    candidate = offer("清风抽纸3层100抽24包", "3层100抽24包20×13cm")
    assert select_sku(candidate, wanted)["match"]["status"] == "matched"
    assert "net_content" not in target_attributes({**wanted, "spec": "24包"})


@pytest.mark.parametrize("text", ["200mm×130mm", "20cm×13cm", "20×13cm", "200*130毫米"])
def test_paper_dimensions_have_one_unit(text: str) -> None:
    assert normalize_value("size", text) == "200x130mm"
    assert parse_attributes(text)["size"] == "200x130mm"


def test_required_fields_follow_category() -> None:
    paper = required_attribute_keys({"category": "抽纸"})
    assert {"layers", "sheets", "size"} <= paper
    assert "net_content" not in paper
    assert {"net_content", "flavor"} <= required_attribute_keys({"category": "坚果"})
    assert "net_content" in required_attribute_keys({"category": "洗发露"})


@pytest.mark.parametrize("flavor", ["山核桃", "奶香", "川香鸡柳", "焦糖", "抹茶", "香葱"])
def test_open_flavor_names_are_not_silently_dropped(flavor: str) -> None:
    title = f"样例牌{flavor}味坚果100g单袋"
    attrs = target_attributes(target(title, category="坚果"))
    assert attrs["flavor"] == flavor
    candidate = offer("样例牌原味坚果100g单袋", "原味100g单袋")
    assert select_sku(candidate, target(title, category="坚果"))["match"]["status"] == "mismatch"


def test_explicit_flavor_label_survives_unrelated_title_terms() -> None:
    assert parse_attributes("口味:香葱; 香辣系列")["flavor"] == "香葱"


def test_sugar_and_fat_are_independent_sku_fields() -> None:
    title = "样例牌原味无糖全脂牛奶250ml×12盒"
    wanted = build_keywords(title, "样例牌").to_target(title)
    attrs = target_attributes(wanted)
    assert attrs["flavor"] == "原味"
    assert attrs["sugar_content"] == "无糖"
    assert attrs["fat_content"] == "全脂"
    assert normalize_value("sugar_content", "0糖") == "无糖"
    assert normalize_value("sugar_content", "0蔗糖") != "无糖"
    candidate = offer("样例牌原味含糖脱脂牛奶250ml×12盒", "原味含糖脱脂250ml×12盒")
    differences = select_sku(candidate, wanted)["match"]["differences"]
    assert {difference["field"] for difference in differences} == {"sugar_content", "fat_content"}


def test_structured_sku_conflict_is_preserved_and_never_approved() -> None:
    wanted = target("样例牌草莓饼干100g单袋")
    candidate = offer(wanted["title"], "100g单袋", attributes={"flavor": "原味"}, vision={"status": "matched"})
    row = candidate["detail"]["skus"][0]
    evidence = row_attribute_evidence(candidate, row, False, wanted)
    assert evidence["attributes"]["flavor"] == "原味"
    assert evidence["conflicts"][0]["field"] == "flavor"
    assert evidence["sources"]["flavor"][-1]["source"] == "sku.attributes"
    selected = select_sku(candidate, wanted)
    assert selected["match"]["status"] == "mismatch"
    assert selected["attribute_conflicts"]
    wanted["user_attributes"] = {"flavor": "原味"}
    assert select_sku(candidate, wanted)["match"]["status"] == "unknown"


def test_brand_and_category_aliases_work_before_vision() -> None:
    wanted = target("飘柔洗发水500ml单瓶", brand="飘柔", category="洗发水")
    candidate = offer("Rejoice洗发露500ml单瓶", "500ml单瓶", attributes={"brand": "Rejoice", "category": "洗发露"})
    assert select_sku(candidate, wanted)["match"]["status"] == "matched"
    assert build_keywords("飘柔洗发露500ml").category == "洗发水"
    assert build_keywords("Rejoice洗发露500ml").brand == "飘柔"
    assert build_keywords("立白绿劲洗洁精500ml").brand == "绿劲"
    assert build_keywords("clearance洗洁精500ml").brand is None


def test_multi_sku_title_does_not_lend_flavor_to_another_row() -> None:
    wanted = target("样例牌草莓饼干100g单袋")
    candidate = offer("样例牌草莓饼干100g单袋", "100g单袋")
    candidate["detail"]["skus"].append({"properties_name": "原味100g单袋", "price": 5})
    assert select_sku(candidate, wanted)["match"]["status"] == "unknown"


def test_custom_aliases_do_not_create_false_evidence_conflicts() -> None:
    wanted = {**target("样例牌草莓饼干100g单袋"), "brand_aliases": ["ExampleCo"]}
    candidate = offer("ExampleCo草莓饼干100g单袋", "100g单袋", attributes={"brand": "ExampleCo"}, vision_attributes={"brand": "样例牌"})
    selected = select_sku(candidate, wanted)
    assert selected["match"]["status"] == "matched"
    assert selected["attribute_conflicts"] == []


def test_refill_variant_is_independent_of_bag_packaging() -> None:
    wanted = target("样例牌洗洁精补充装500ml×2袋", category="洗洁精")
    expected = target_attributes(wanted)
    assert expected["packaging"] == "袋装"
    assert expected["packaging_variant"] == "补充装"
    candidate = offer("样例牌洗洁精正装500ml×2袋", "正装500ml×2袋")
    differences = select_sku(candidate, wanted)["match"]["differences"]
    assert {difference["field"] for difference in differences} == {"packaging_variant"}
    assert parse_attributes("包装类型:补充装;500ml×2袋")["packaging"] == "袋装"
    assert parse_attributes("包装类型:补充装;500ml×2袋")["packaging_variant"] == "补充装"


def test_legacy_packaging_property_is_routed_to_variant() -> None:
    wanted = target("样例牌洗洁精补充装500ml×2袋", category="洗洁精")
    candidate = offer("样例牌洗洁精500ml×2袋", "500ml×2袋", attributes={"packaging": "补充装"})
    selected = select_sku(candidate, wanted)
    assert selected["match"]["status"] == "matched"
    assert selected["attributes"]["packaging"] == "袋装"
    assert selected["attributes"]["packaging_variant"] == "补充装"


def test_foam_and_spray_are_distinct_forms() -> None:
    assert parse_attributes("剂型:泡沫;500ml单瓶")["form"] == "泡沫"
    wanted = target("样例牌泡沫洗洁精500ml单瓶", category="洗洁精")
    candidate = offer("样例牌喷雾洗洁精500ml单瓶", "喷雾500ml单瓶")
    assert select_sku(candidate, wanted)["match"]["status"] == "mismatch"
    assert build_keywords("样例牌泡沫洗洁精500ml单瓶", "样例牌").form == "泡沫"
