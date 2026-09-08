from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from attribute_schema import brand_aliases, canonical_name, category_aliases, CATEGORY_WORDS, required_attribute_keys
from identifier_sources import identifiers_from_data
from product_identifiers import compare_identifiers, normalize_identifiers


ATTRIBUTE_KEYS = ("brand", "category", "series", "flavor", "color", "form", "net_content", "pack_count", "packaging", "packaging_variant", "pack_structure", "model", "layers", "sheets", "size", "sugar_content", "fat_content", "sales_unit", "gift_count")
FLAVORS = ("红烧牛肉", "香辣牛肉", "酸辣牛肉", "葱香排骨", "青柠薄荷", "柚子金桔", "柚子柠檬", "金桔柠檬", "巧克力", "草莓", "香草", "柠檬", "薄荷", "柚子", "金桔", "葡萄", "蓝莓", "芒果", "蜜桃", "水蜜桃", "原味", "香辣", "麻辣", "五香", "海盐", "番茄", "黄瓜", "橙味", "奶油", "焦糖", "抹茶", "酸辣", "蜂蜜")
COLORS = ("黑色", "白色", "红色", "蓝色", "黄色", "绿色", "灰色", "粉色", "紫色", "棕色", "金色", "银色", "卡其", "藏青", "透明", "自然色")
FIELD_LABELS = {"品牌": "brand", "品类": "category", "系列": "series", "口味": "flavor", "香型": "flavor", "颜色": "color", "形态": "form", "剂型": "form", "净含量": "net_content", "单件净含量": "net_content", "包装数量": "pack_count", "件数": "pack_count", "包装类型": "packaging", "销售包装类型": "packaging_variant", "型号": "model", "糖分": "sugar_content", "含糖量": "sugar_content", "脂肪": "fat_content", "销售单位": "sales_unit", "计价单位": "sales_unit", "赠品数量": "gift_count"}
PACKAGING_VARIANTS = ("补充装", "替换装", "礼盒装", "礼盒", "试用装", "旅行装", "体验装", "正装", "普通装")
FORM_VALUES = ("液体", "固体", "膏体", "粉末", "颗粒", "凝珠", "胶囊", "泡沫", "喷雾")
MEASURE_PATTERN = r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kg|公斤|千克|mg|毫克|g|克|斤|两|ml|毫升|l|升)(?![a-zA-Z])"
PACK_UNITS = r"袋|瓶|盒|罐|包|支|片|个|件|桶|杯|碗|箱|提|卷"
SIZE_PATTERN = r"(\d+(?:\.\d+)?)\s*(cm|mm|厘米|毫米)?\s*[*xX×]\s*(\d+(?:\.\d+)?)\s*(cm|mm|厘米|毫米)"


def number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
        return float(result) if result.is_finite() else None
    except InvalidOperation:
        return None


def measure(value: Any) -> tuple[str, Decimal] | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(kg|公斤|千克|mg|毫克|g|克|斤|两|ml|毫升|l|升)\s*", str(value or ""), re.I)
    if not match:
        return None
    unit = match[2].lower()
    dimension = "volume" if unit in {"ml", "毫升", "l", "升"} else "mass"
    multiplier = {"kg": 1000, "公斤": 1000, "千克": 1000, "l": 1000, "升": 1000, "mg": Decimal(".001"), "毫克": Decimal(".001"), "斤": 500, "两": 50}.get(unit, 1)
    return dimension, Decimal(match[1]) * multiplier


def normalize_value(key: str, value: Any) -> Any:
    if key in {"brand", "category"}:
        return canonical_name(key, value)
    if key == "flavor":
        cleaned = re.sub(r"\s+", "", str(value or "")).lower()
        for flavor in sorted(FLAVORS, key=len, reverse=True):
            if cleaned in {flavor, flavor + "味", flavor + "口味", flavor + "香型"}:
                return flavor
        if cleaned not in {"原味", "美味", "风味"}:
            return re.sub(r"(?:口味|香型|味)$", "", cleaned) or None
    if key == "packaging":
        cleaned = str(value or "").strip()
        if cleaned in {"袋", "瓶", "盒", "罐", "桶", "杯", "碗", "包", "支", "片", "卷", "箱", "件", "提"}:
            return cleaned + "装"
    if key == "sales_unit":
        return re.sub(r"^(?:每|按|/)|(?:装|销售|计价)$", "", str(value or "").strip()) or None
    if key == "packaging_variant":
        return "礼盒" if str(value or "").strip() == "礼盒装" else str(value or "").strip() or None
    if key == "net_content":
        parsed = measure(value)
        if parsed:
            amount = format(parsed[1], "f")
            if "." in amount:
                amount = amount.rstrip("0").rstrip(".")
            return amount + ("g" if parsed[0] == "mass" else "ml")
        return None
    if key in {"pack_count", "gift_count", "layers", "sheets"}:
        n = number(value)
        if n is None:
            hit = re.fullmatch(rf"\s*(\d+)\s*(?:{PACK_UNITS}|层|抽)?\s*", str(value or ""))
            n = float(hit[1]) if hit else None
        valid = n is not None and n.is_integer() and (n > 0 or key == "gift_count" and n == 0)
        return (int(n) if key in {"pack_count", "gift_count"} else str(int(n))) if valid else None
    if key == "size":
        hit = re.fullmatch(SIZE_PATTERN, str(value or "").strip(), re.I)
        if hit:
            sizes = [Decimal(hit[1]) * (10 if (hit[2] or hit[4]).lower() in {"cm", "厘米"} else 1), Decimal(hit[3]) * (10 if hit[4].lower() in {"cm", "厘米"} else 1)]
            return "x".join(format(v.normalize(), "f") for v in sizes) + "mm"
    if key == "sugar_content":
        clean = re.sub(r"\s+", "", str(value or "")).lower()
        return {"零糖": "无糖", "0糖": "无糖", "0蔗糖": "无蔗糖", "零蔗糖": "无蔗糖", "不添加蔗糖": "无添加蔗糖", "不添加糖": "无添加糖"}.get(clean, clean) or None
    return re.sub(r"\s+", "", str(value or "")).lower() or None


def attribute_field(key: str, value: Any) -> str:
    return "packaging_variant" if key == "packaging" and str(value or "").strip() in PACKAGING_VARIANTS else key


def _one_word(text: str, words: tuple[str, ...]) -> str | None:
    found = [w for w in words if w in text]
    found = [w for w in found if not any(w != other and w in other for other in found)]
    return found[0] if len(found) == 1 else None


def _flavor_from_text(text: str) -> str | None:
    cleaned = text
    for category in sorted(CATEGORY_WORDS, key=len, reverse=True):
        cleaned = cleaned.replace(category, " ")
    cleaned = re.sub(r"无添加蔗糖|无添加糖|无蔗糖|零蔗糖|0蔗糖|无糖|零糖|0糖|低糖|含糖|全脂|低脂|脱脂", " ", cleaned)
    found = [word for word in FLAVORS if word in cleaned]
    for hit in re.finditer(r"([\u4e00-\u9fff]{1,10}?)(?:口味|香型|味)", cleaned):
        value = re.sub(r"^(?:散装|独立包装|混合|经典|浓郁|香浓)", "", hit[0])
        if value not in {"美味", "风味", "口味", "多种口味", "多口味"}:
            found.append(normalize_value("flavor", value))
    found = list(dict.fromkeys(v for v in found if v))
    found = [v for v in found if not any(v != other and v in other for other in found)]
    return found[0] if len(found) == 1 else None


def _pack_attributes(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    gifts = list(re.finditer(rf"(?:赠送?|送)\s*(?:\d+(?:\.\d+)?\s*(?:g|克|ml|毫升)\s*)?(\d+)\s*({PACK_UNITS})?(?![\d.a-zA-Z]|克|毫升|升)", text, re.I))
    if gifts:
        out["gift_count"] = sum(int(hit[1]) for hit in gifts)
    clean = re.split(r"[+＋]?\s*(?:赠送?|送)", text, maxsplit=1)[0]
    chain = re.search(rf"(?:kg|公斤|千克|mg|毫克|g|克|斤|两|ml|毫升|l|升)\s*[*xX×]\s*(\d+)\s*({PACK_UNITS})?", clean, re.I)
    if not chain:
        chain = re.search(rf"(?<![\d.])(\d+)\s*({PACK_UNITS})\s*(?:装)?", clean)
    if chain:
        levels = [(int(chain[1]), chain[2] or "件")]
        tail = clean[chain.end():]
        while nested := re.match(rf"\s*(?:/\s*(?:盒|箱|提))?\s*[*xX×]\s*(\d+)\s*({PACK_UNITS})", tail):
            levels.append((int(nested[1]), nested[2]))
            tail = tail[nested.end():]
        if len(levels) == 1 and (per_box := re.search(r"每(盒|箱|提)", clean[:chain.start()])):
            if outer := re.match(rf"\s*[,，;；]\s*(\d+)\s*({per_box[1]})\s*装?", tail):
                levels.append((int(outer[1]), outer[2]))
                tail = tail[outer.end():]
        if len({unit for _, unit in levels}) == len(levels):
            levels.sort(key=lambda level: {"盒": 1, "提": 2, "箱": 3}.get(level[1], 0))
        out["pack_count"] = math.prod(count for count, _ in levels)
        if chain[2]:
            out["packaging"] = normalize_value("packaging", levels[0][1])
        if len(levels) > 1:
            out["pack_structure"] = "*".join(f"{count}{unit}" for count, unit in levels)
        per = re.match(r"\s*/\s*(箱|盒|提|组|件)", tail)
        if per:
            out["sales_unit"] = per[1]
    buy = re.search(rf"买\s*(\d+)\s*({PACK_UNITS})?", clean)
    if buy and "pack_count" not in out:
        out["pack_count"] = int(buy[1])
        if buy[2]:
            out["packaging"] = normalize_value("packaging", buy[2])
    single = re.search(r"单([袋瓶盒罐包支件])", clean)
    if single and "pack_count" not in out:
        out["pack_count"] = 1
        out["packaging"] = normalize_value("packaging", single[1])
    packaging = _one_word(clean, ("袋装", "瓶装", "盒装", "罐装", "桶装", "杯装", "碗装"))
    if packaging and "packaging" not in out:
        out["packaging"] = packaging
    variant = _one_word(clean, PACKAGING_VARIANTS)
    if variant:
        out["packaging_variant"] = normalize_value("packaging_variant", variant)
    sale = re.search(r"(?:销售单位|计价单位)\s*[:：]\s*(袋|瓶|盒|罐|包|支|片|个|件|箱|组|套|提|卷)|按(袋|瓶|盒|罐|包|支|片|个|件|箱|组|套|提|卷)(?:销售|计价)", clean)
    if sale:
        out["sales_unit"] = sale[1] or sale[2]
    elif outer := re.search(r"整(箱|盒|提|件)(?:装)?", clean):
        out["sales_unit"] = outer[1]
    return out


def parse_attributes(text: str, brand: str = "") -> dict[str, Any]:
    text = str(text or "")
    clean = text
    for alias in sorted(brand_aliases(brand) if brand else [], key=len, reverse=True):
        if alias:
            clean = re.sub(re.escape(alias), " ", clean, flags=re.I)
    clean = re.split(r"[+＋]?\s*(?:赠送?|送)", clean, maxsplit=1)[0]
    out: dict[str, Any] = {}
    for label, key in FIELD_LABELS.items():
        hit = re.search(rf"{label}\s*[:：]\s*([^;；,，\n]+)", clean)
        if hit:
            out[attribute_field(key, hit[1])] = hit[1].strip()
    flavor = _flavor_from_text(clean)
    if flavor:
        out.setdefault("flavor", flavor)
    for key, words in (("color", COLORS), ("form", FORM_VALUES), ("sugar_content", ("无添加蔗糖", "无添加糖", "无蔗糖", "零蔗糖", "0蔗糖", "无糖", "零糖", "0糖", "低糖", "含糖")), ("fat_content", ("全脂", "低脂", "脱脂"))):
        value = _one_word(clean, words)
        if value:
            out.setdefault(key, value)
    weight_text = re.sub(r"(?:总净含量|总重量|总重|合计)\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:kg|公斤|千克|mg|毫克|g|克|斤|两|ml|毫升|l|升)", "", clean, flags=re.I)
    per_item = re.search(r"(?:单件|单袋|单瓶|单盒|每袋|每瓶|每盒)(?:净含量|容量|重量)?\s*[:：]?\s*" + MEASURE_PATTERN, weight_text, re.I)
    specs = re.findall(MEASURE_PATTERN, weight_text, re.I)
    normalized = {normalize_value("net_content", "".join(spec)) for spec in specs}
    if per_item:
        out["net_content"] = normalize_value("net_content", per_item[1] + per_item[2])
    elif len(normalized) == 1:
        out.setdefault("net_content", normalized.pop())
    for key, value in _pack_attributes(text).items():
        out.setdefault(key, value)
    for pattern, key in ((r"(\d+)\s*层", "layers"), (r"(\d+)\s*抽", "sheets")):
        hit = re.search(pattern, clean, re.I)
        if hit:
            out[key] = hit[1]
    size = re.search(SIZE_PATTERN, clean, re.I)
    if size:
        out["size"] = size[0]
    if "湿巾" in clean and (sheets := re.search(r"(\d+)\s*片", clean)):
        out["sheets"] = sheets[1]
    for label in ("系列", "型号"):
        hit = re.search(rf"{label}[:：]\s*([^;；,，\s]+)", clean)
        if hit:
            out[FIELD_LABELS[label]] = hit[1]
    return {k: normalized for k, v in out.items() if (normalized := normalize_value(k, v)) is not None}


def target_attributes(target: dict[str, Any]) -> dict[str, Any]:
    brand = str(target.get("brand") or "")
    attrs = parse_attributes(str(target.get("title") or ""), brand)
    for key in ATTRIBUTE_KEYS:
        value = (target.get("net_content") or target.get("spec")) if key == "net_content" else target.get(key)
        if value not in (None, "", [], {}):
            field = attribute_field(key, value)
            if (normalized := normalize_value(field, value)) is not None:
                attrs[field] = normalized
    attrs.update(parse_attributes(" ".join(str(x) for x in target.get("variant") or []), brand))
    attrs.update(parse_attributes(str(target.get("selected_sku") or ""), brand))
    for layer in (target.get("attributes"), target.get("user_attributes")):
        if isinstance(layer, dict):
            attrs.update({attribute_field(k, v): normalized for k, v in layer.items() if k in ATTRIBUTE_KEYS and (normalized := normalize_value(attribute_field(k, v), v)) is not None})
    return {k: v for k, v in attrs.items() if v is not None}


def sku_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    raw = detail.get("skus") or detail.get("sku") or []
    if isinstance(raw, dict):
        raw = raw.get("sku") or raw.get("list") or []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def sku_text(row: dict[str, Any]) -> str:
    return str(row.get("properties_name") or row.get("name") or row.get("skuName") or "")


def compare_attributes(target: dict[str, Any], candidate: dict[str, Any], aliases: list[str] | None = None, allow_pack: bool = False) -> dict[str, Any]:
    differences, missing, matched = [], [], []
    for key, expected in target.items():
        if key not in ATTRIBUTE_KEYS or key == "gift_count" or expected in (None, "") or normalize_value(key, expected) is None:
            continue
        actual = candidate.get(key)
        if actual in (None, ""):
            missing.append(key)
            continue
        same = normalize_value(key, expected) == normalize_value(key, actual)
        if key == "brand" and aliases:
            same = normalize_value(key, actual) in {normalize_value(key, v) for v in [expected, *aliases]}
        if same:
            matched.append(key)
        elif not (key == "pack_count" and allow_pack):
            differences.append({"field": key, "target": expected, "candidate": actual})
    return {"status": "mismatch" if differences else "unknown" if missing else "matched", "differences": differences, "missing": missing, "matched": matched}


def row_attribute_evidence(item: dict[str, Any], row: dict[str, Any], multi: bool, target: dict[str, Any]) -> dict[str, Any]:
    detail = item.get("detail") or {}
    brand = str(target.get("brand") or "")
    title = str(item.get("title") or detail.get("title") or "")
    common = {"brand", "category", "series", "form", "model"}
    attrs: dict[str, Any] = {}
    sources: dict[str, list[dict[str, Any]]] = {}
    conflicts: list[dict[str, Any]] = []
    known_aliases = [brand, str((target.get("attributes") or {}).get("brand") or ""), *(target.get("brand_aliases") or [])]
    def merge(values: dict[str, Any], source: str, text: str = "") -> None:
        for key, value in values.items():
            key = attribute_field(key, value)
            if key not in ATTRIBUTE_KEYS or (value := normalize_value(key, value)) is None:
                continue
            evidence = {"source": source, "value": value, "text": text}
            if key in attrs and compare_attributes({key: attrs[key]}, {key: value}, known_aliases)["differences"]:
                conflicts.append({"field": key, "previous": attrs[key], "value": value, "previous_source": sources[key][-1]["source"], "source": source})
            sources.setdefault(key, []).append(evidence)
            attrs[key] = value
    title_attrs = parse_attributes(title, brand)
    if multi:
        title_attrs = {key: value for key, value in title_attrs.items() if key in common}
    for key, words in (("brand", [*brand_aliases(brand), *(target.get("brand_aliases") or [])]), ("category", category_aliases(str(target.get("category") or "")))):
        for word in words:
            if word and re.sub(r"\s+", "", word).lower() in re.sub(r"\s+", "", title).lower():
                title_attrs.setdefault(key, word)
                break
    merge(title_attrs, "title", title)
    props = detail.get("props") or []
    if isinstance(props, list):
        for prop in props:
            if isinstance(prop, dict) and prop.get("name") in FIELD_LABELS:
                key = FIELD_LABELS[prop["name"]]
                if not multi or key in common:
                    merge({key: prop.get("value")}, "detail.props", str(prop.get("value") or ""))
    merge(parse_attributes(sku_text(row), brand), "sku.text", sku_text(row))
    for name in ("attributes", "vision_attributes"):
        if isinstance(row.get(name), dict):
            merge(row[name], "sku." + name)
    return {"attributes": attrs, "sources": sources, "conflicts": conflicts}


def row_attributes(item: dict[str, Any], row: dict[str, Any], multi: bool, target: dict[str, Any]) -> dict[str, Any]:
    return row_attribute_evidence(item, row, multi, target)["attributes"]


def stock_of(row: dict[str, Any]) -> int | None:
    for key in ("quantity", "amountOnSale", "num", "stock"):
        value = number(row.get(key))
        if value is not None:
            return max(0, int(value))
    return None


def price_quote(row: dict[str, Any], quantity: int, detail: dict[str, Any], single: bool, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    from sourcing_costs import unit
    attrs = attributes or {}
    quote_sources = [row, detail] if single or detail.get("price_scope") == "all_skus" else [row]
    def value(*keys: str) -> Any:
        return next((source[key] for source in quote_sources for key in keys if source.get(key) not in (None, "")), None)
    quote_unit = unit(value("priceUnit", "price_unit", "sales_unit", "unitName", "unit"))
    sales_unit = unit(attrs.get("sales_unit") or value("sales_unit", "unitName", "unit") or quote_unit)
    missing = []
    multiplier = 1
    stock_multiplier = moq_multiplier = 1
    if quote_unit and sales_unit and quote_unit != sales_unit:
        base_unit = unit(attrs.get("packaging"))
        if quote_unit == base_unit and attrs.get("pack_count"):
            multiplier = int(attrs["pack_count"])
            stock_unit = unit(value("stock_unit", "inventory_unit"))
            moq_unit = unit(value("moq_unit", "min_order_unit"))
            stock_multiplier = multiplier if stock_unit == quote_unit else 1 if stock_unit == sales_unit else None
            moq_multiplier = multiplier if moq_unit == quote_unit else 1 if moq_unit == sales_unit else None
            if stock_multiplier is None:
                missing.append("库存单位换算待确认")
            if moq_multiplier is None:
                missing.append("起批单位换算待确认")
        else:
            missing.append("报价单位与销售单位不一致")
    priced_quantity = quantity * multiplier
    price = next((number(row[key]) for key in ("price", "unitPrice", "salePrice") if number(row.get(key)) is not None), None)
    source = "sku.price" if price is not None else ""
    tiers = row.get("price_tiers") or row.get("priceRanges") or []
    if not tiers and (single or detail.get("price_scope") == "all_skus"):
        tiers = detail.get("price_tiers") or detail.get("priceRange") or detail.get("priceRanges") or []
    eligible = []
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            minimum = number(tier.get("startQuantity", tier.get("beginAmount", tier.get("start", tier.get("min_num", 1)))))
            maximum = number(tier.get("endQuantity", tier.get("maxQuantity", tier.get("end", tier.get("max_num")))))
            tier_price = number(tier.get("price"))
            if minimum is not None and tier_price is not None and tier_price > 0 and minimum <= priced_quantity and (maximum is None or priced_quantity <= maximum):
                eligible.append((minimum, tier_price, tier))
    applied_tier = None
    if eligible:
        _, price, applied_tier = max(eligible, key=lambda item: item[0])
        source = "sku.tier" if row.get("price_tiers") or row.get("priceRanges") else "detail.tier"
    elif tiers:
        price = None
        missing.append("采购量未命中有效阶梯价")
    if price is None and not tiers and single:
        price = number(detail.get("price"))
        source = "single_sku.detail_price" if price is not None else ""
    if price is None or price <= 0:
        price = None
    if "报价单位与销售单位不一致" in missing:
        price = None
    currency = value("currency", "price_currency", "priceCurrency")
    if not currency and "元" in str(value("priceUnit", "price_unit") or ""):
        currency = "CNY"
    return {"price": price, "source": source, "quoted_unit": quote_unit or None, "sales_unit": sales_unit or None, "currency": currency,
            "pricing_quantity": priced_quantity, "quantity_multiplier": multiplier, "stock_multiplier": stock_multiplier, "moq_multiplier": moq_multiplier, "applied_tier": applied_tier, "missing": missing}


def price_of(row: dict[str, Any], quantity: int, detail: dict[str, Any], single: bool) -> float | None:
    return price_quote(row, quantity, detail, single)["price"]


def select_sku(item: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    expected = target_attributes(target)
    target_identifiers = identifiers_from_data(target, "target")
    detail = item.get("detail") or {}
    rows = sku_rows(detail)
    real_rows = bool(rows)
    if not rows:
        rows = [{"properties_name": item.get("skuName") or item.get("sku_name") or "", "price": item.get("unitPrice", item.get("price")), "quantity": stock_of(detail)}]
    choices = []
    quantity = max(1, int(number(target.get("buy_multiple") or target.get("quantity")) or 1))
    for index, row in enumerate(rows):
        evidence = row_attribute_evidence(item, row, len(rows) > 1, target)
        attrs = evidence["attributes"]
        match = compare_attributes(expected, attrs, target.get("brand_aliases"), bool(target.get("allow_pack_substitution")))
        if evidence["conflicts"]:
            if match["status"] != "mismatch":
                match["status"] = "unknown"
            match["missing"].extend("conflict." + entry["field"] for entry in evidence["conflicts"])
            match["conflicts"] = evidence["conflicts"]
        if target.get("unconfirmed_attributes"):
            if match["status"] != "mismatch":
                match["status"] = "unknown"
            match["missing"].extend("target." + key for key in target["unconfirmed_attributes"])
        vision = row.get("vision") or {}
        if vision.get("status") == "mismatch":
            match["status"] = "mismatch"
            match["differences"].extend(vision.get("differences") or [{"field": "image", "target": "一致", "candidate": "图片存在冲突"}])
        elif target.get("require_vision") and vision.get("status") != "matched":
            if match["status"] != "mismatch":
                match["status"] = "unknown"
            match["missing"].append("image_verification")
        candidate_identifiers = identifiers_from_data(row, "sku")
        if len(rows) == 1:
            candidate_identifiers = normalize_identifiers([*candidate_identifiers, *identifiers_from_data(detail, "detail"), *identifiers_from_data(item, "offer")])
        identifier_comparison = compare_identifiers(target_identifiers, candidate_identifiers)
        if identifier_comparison["status"] == "conflict":
            if match["status"] != "mismatch":
                match["status"] = "unknown"
            match["missing"].append("同包装层级条码冲突待核实")
            match["identifier_conflicts"] = identifier_comparison["conflicts"]
        ratio = Decimal(str(expected.get("pack_count") or 1)) / Decimal(str(attrs.get("pack_count") or 1)) if target.get("allow_pack_substitution") else Decimal(1)
        order_qty = math.ceil(Decimal(quantity) * ratio)
        moq = next((number(source.get(key)) for source in (row, item, detail) for key in ("minOrderQuantity", "MOQ", "moq", "min_num") if number(source.get(key)) is not None), None)
        quote = price_quote(row, order_qty, detail, len(rows) == 1, attrs)
        price = quote["price"]
        stock = stock_of(row)
        if stock is None and stock_of(detail) == 0:
            stock = 0
        quote["reported_stock"] = stock
        quote["reported_moq"] = moq
        if stock is not None and quote.get("stock_multiplier"):
            stock = math.floor(stock / quote["stock_multiplier"])
        elif quote.get("stock_multiplier") is None and stock:
            stock = None
        if moq is not None and quote.get("moq_multiplier"):
            moq = math.ceil(moq / quote["moq_multiplier"])
        elif quote.get("moq_multiplier") is None and moq:
            moq = None
        if not real_rows:
            match["status"] = "unknown" if expected and match["status"] == "matched" else match["status"]
            match["missing"].append("sku_detail")
        total = float(Decimal(str(price)) * quote["pricing_quantity"]) if price is not None else None
        choices.append({"sku_id": str(row.get("sku_id") or row.get("skuId") or ""), "sku_index": index, "sku_name": sku_text(row), "sku_image_url": str(row.get("sku_image_url") or row.get("image") or row.get("pic_url") or (item.get("pic_url") if len(rows) == 1 else "") or ""), "attributes": attrs, "attribute_sources": evidence["sources"], "attribute_conflicts": evidence["conflicts"], "match": match, "unit_price": total / quantity if total is not None else None, "sku_price": price, "stock": stock, "moq": moq, "order_quantity": order_qty, "purchase_total": total, "vision": vision, "sales_unit": quote["sales_unit"], "price_evidence": quote})
        from sourcing_costs import evaluate_purchase
        choices[-1]["purchase"] = evaluate_purchase(item, target, choices[-1], row)
        choices[-1].update(target_identifiers=target_identifiers, identifiers=candidate_identifiers, identifier_comparison=identifier_comparison)
    def priority(c: dict) -> tuple:
        insufficient = (c["stock"] is not None and c["stock"] < c["order_quantity"]) or (c["moq"] is not None and c["moq"] > c["order_quantity"])
        return ({"matched": 0, "unknown": 1, "mismatch": 2}[c["match"]["status"]], len(c["match"]["differences"]), len(c["match"]["missing"]), insufficient, {"confirmed": 0, "pending": 1, "unavailable": 2}[c["purchase"]["status"]], c["sku_price"] is None, c["purchase"].get("landed_unit") or c["unit_price"] or float("inf"))
    return min(choices, key=priority)


def relevance(item: dict[str, Any], target: dict[str, Any]) -> float:
    title = str(item.get("title") or "").lower()
    attrs = target_attributes(target)
    score = sum(4 if key == "brand" else 2 for key, value in attrs.items() if key != "pack_count" and str(value).lower() in title)
    if "image" in (item.get("sources") or []):
        score += 2
    return score
