from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


STATUS_LABELS = {"confirmed": "已确认可采购", "pending": "可购性待确认", "unavailable": "当前不可采购"}


def amount(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def money(value: Decimal | None) -> float | None:
    return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) if value is not None else None


def unit(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^(?:人民币|元|rmb|cny)?\s*/\s*|^(?:每|按)|(?:装|销售|计价)$", "", text)
    return {"piece": "件", "pieces": "件", "bottle": "瓶", "bag": "袋", "box": "盒", "carton": "箱"}.get(text, text)


def _currency(value: Any) -> str:
    text = str(value or "").upper()
    return "CNY" if text in {"CNY", "RMB", "人民币", "元"} else text


def _value(sources: list[dict], *keys: str) -> Any:
    return next((source[key] for source in sources for key in keys if source.get(key) not in (None, "")), None)


def _regions(value: Any) -> set[str]:
    if isinstance(value, dict):
        value = [value.get(key) for key in ("province", "city", "district", "region")]
    elif isinstance(value, str):
        value = re.split(r"[,，/、;；\s]+", value)
    if not isinstance(value, list):
        return set()
    aliases = {"新疆维吾尔自治区": "新疆", "广西壮族自治区": "广西", "宁夏回族自治区": "宁夏", "内蒙古自治区": "内蒙古", "西藏自治区": "西藏"}
    return {re.sub(r"(?:省|市)$", "", aliases.get(str(entry).strip(), str(entry).strip())) for entry in value if entry}


def _quote_errors(quote: dict, target: dict, selected: dict, *, quantity_required: bool = True) -> list[str]:
    errors = []
    if quote.get("confirmed") is not True:
        errors.append("报价未确认")
    quoted_quantity = amount(quote.get("quantity"))
    if quantity_required and quoted_quantity is None:
        errors.append("报价未绑定采购量")
    if quoted_quantity is not None and quoted_quantity != Decimal(str(selected["order_quantity"])):
        errors.append("报价采购量不一致")
    if quote.get("sku_id") is not None and str(quote["sku_id"]) != str(selected.get("sku_id") or ""):
        errors.append("报价SKU不一致")
    expires = amount(quote.get("expires_at"))
    if expires is not None and float(expires) <= time.time():
        errors.append("报价已过期")
    currency = quote.get("currency")
    expected_currency = selected.get("price_evidence", {}).get("currency")
    if currency and expected_currency and _currency(currency) != _currency(expected_currency):
        errors.append("报价币种不一致")
    return errors


def _region_errors(rule: dict, target: dict) -> list[str]:
    destination = _regions(target.get("destination"))
    includes = _regions(rule.get("regions") or rule.get("destination"))
    excludes = _regions(rule.get("excluded_regions"))
    if rule.get("scope") == "nationwide" and not excludes and not rule.get("destination") and not rule.get("regions"):
        return []
    if not destination:
        return ["收货地区待确认"]
    binding = rule.get("destination")
    if isinstance(binding, dict):
        actual = target.get("destination")
        if not isinstance(actual, dict):
            return ["报价省市区范围待确认"]
        for key in ("province", "city", "district"):
            if binding.get(key) and _regions([binding[key]]) != _regions([actual.get(key)]):
                return ["运费报价地区不一致"]
    if excludes & destination:
        return ["收货地区不适用此运费规则"]
    if includes and not includes & destination:
        return ["运费报价地区不一致"]
    if not includes and rule.get("scope") != "nationwide":
        return ["运费报价未绑定收货地区"]
    return []


def _shipping(sources: list[dict], target: dict, selected: dict, merchandise: Decimal | None, discount: Decimal | None = Decimal(0)) -> tuple[Decimal | None, list[str], dict]:
    quote = _value(sources, "shipping_quote")
    if isinstance(quote, dict):
        errors = _quote_errors(quote, target, selected) + _region_errors(quote, target)
        value = amount(quote.get("amount"))
        if value is None:
            errors.append("运费金额待确认")
        return (None if errors else value), errors, {"source": "shipping_quote", "quote": quote}
    rule = _value(sources, "shipping_rules")
    if isinstance(rule, dict):
        errors = _quote_errors(rule, target, selected, quantity_required=False) + _region_errors(rule, target)
        value = None
        threshold = amount(rule.get("free_threshold"))
        threshold_amount = merchandise
        if threshold is not None and discount != 0:
            if rule.get("threshold_basis") == "after_discount" and merchandise is not None and discount is not None:
                threshold_amount = merchandise - discount
            elif rule.get("threshold_basis") != "before_discount":
                errors.append("免邮门槛计算口径待确认")
        if threshold is not None and threshold_amount is not None and threshold_amount >= threshold:
            value = Decimal(0)
        elif rule.get("free") is True and threshold is None:
            value = Decimal(0)
        elif amount(rule.get("amount")) is not None:
            value = amount(rule["amount"])
        else:
            basis = rule.get("basis")
            count = Decimal(str(selected["order_quantity"])) if basis == "sku_quantity" else amount(_value(sources, "shipping_weight_g")) if basis == "weight_g" else None
            first, extra = amount(rule.get("first_units")), amount(rule.get("additional_units"))
            first_fee, extra_fee = amount(rule.get("first_fee")), amount(rule.get("additional_fee"))
            if count is not None and first is not None and first_fee is not None and count <= first:
                value = first_fee
            elif count is not None and first is not None and first_fee is not None and extra is not None and extra > 0 and extra_fee is not None:
                value = first_fee + math.ceil((count - first) / extra) * extra_fee
        if value is None:
            errors.append("运费条件或续费金额待确认")
        return (None if errors else value), errors, {"source": "shipping_rules", "rule": rule}
    freight = _value(sources, "freightInfo")
    if isinstance(freight, dict) and freight.get("confirmed") is True:
        normalized = {**freight, "amount": freight.get("totalCost", freight.get("amount"))}
        return _shipping([{"shipping_quote": normalized}], target, selected, merchandise, discount)
    quoted = _value(sources, "shipping", "shipping_fee", "freight_fee", "post_fee", "express_fee")
    free = _value(sources, "freeShipping", "free_shipping", "freeDeliverFee", "postFree", "marketingFreePostage")
    note = "免邮适用地区及门槛待确认" if free is True or str(free).lower() in {"true", "1", "包邮"} else "运费待确认"
    return None, [note], {"source": "unbound_shipping", "quoted_value": quoted, "free_signal": free}


def _discount(sources: list[dict], target: dict, selected: dict, merchandise: Decimal | None) -> tuple[Decimal | None, list[str], dict]:
    quote = _value(sources, "discount_quote")
    if not isinstance(quote, dict):
        return Decimal(0), [], {"source": "no_discount_applied"}
    if quote.get("included_in_price") is True:
        return Decimal(0), [], {"source": "included_in_quoted_price"}
    errors = _quote_errors(quote, target, selected)
    if quote.get("applied") is not True or quote.get("applies_to") != "merchandise":
        errors.append("优惠适用状态待确认")
    minimum = amount(quote.get("minimum_amount"))
    if minimum is not None and (merchandise is None or merchandise < minimum):
        errors.append("优惠门槛未满足")
    discount = amount(quote.get("amount"))
    if discount is None or merchandise is None or discount > merchandise:
        errors.append("优惠金额待确认")
    return (None if errors else discount), errors, {"source": "discount_quote", "quote": quote}


def _tax(sources: list[dict], taxable: Decimal | None, target: dict, selected: dict) -> tuple[Decimal | None, list[str], dict]:
    included = _value(sources, "price_tax_included", "tax_included")
    if included is True:
        return Decimal(0), [], {"source": "price_tax_included", "included": True}
    if included is not False:
        return None, ["报价是否含税待确认"], {"source": "tax_unknown"}
    quote = _value(sources, "tax_quote")
    if isinstance(quote, dict):
        errors = _quote_errors(quote, target, selected)
        tax = amount(quote.get("amount"))
        if tax is None:
            errors.append("额外税费金额待确认")
        return (None if errors else tax), errors, {"source": "tax_quote", "quote": quote, "included": False}
    tax = amount(_value(sources, "tax_amount"))
    scope = _value(sources, "tax_scope")
    quoted_quantity = amount(_value(sources, "tax_quantity"))
    if tax is not None and scope == "order" and quoted_quantity == Decimal(str(selected["order_quantity"])):
        return tax, [], {"source": "tax_amount", "included": False}
    rate = amount(_value(sources, "tax_rate"))
    if rate is not None and rate <= 100 and taxable is not None and _value(sources, "tax_basis") == "merchandise":
        rate = rate / 100 if rate > 1 else rate
        return taxable * rate, [], {"source": "tax_rate", "rate": float(rate), "included": False}
    return None, ["额外税费待确认"], {"source": "tax_unknown", "included": False}


def evaluate_purchase(item: dict, target: dict, selected: dict, row: dict | None = None) -> dict:
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    sources = [row or {}, detail, item]
    missing, reasons = [], []
    order_quantity = Decimal(str(selected.get("order_quantity") or 1))
    stock = amount(selected.get("stock"))
    moq = amount(selected.get("moq"))
    if stock is None:
        missing.append("SKU库存待确认")
    elif stock < order_quantity:
        reasons.append("SKU库存不足")
    if moq is None:
        missing.append("起批数待确认")
    elif moq > order_quantity:
        reasons.append("采购量低于起批数")
    sales_unit = unit(selected.get("sales_unit"))
    if not sales_unit:
        missing.append("销售单位待确认")
    price_evidence = selected.get("price_evidence") or {}
    missing.extend(price_evidence.get("missing") or [])
    merchandise = amount(selected.get("purchase_total"))
    if merchandise is None:
        missing.append("采购量对应价格待确认")
    currency = _currency(price_evidence.get("currency"))
    if currency != "CNY":
        missing.append("人民币报价待确认")
    discount, discount_missing, discount_evidence = _discount(sources, target, selected, merchandise)
    shipping, shipping_missing, shipping_evidence = _shipping(sources, target, selected, merchandise, discount)
    taxable = merchandise - discount if merchandise is not None and discount is not None else None
    tax, tax_missing, tax_evidence = _tax(sources, taxable, target, selected)
    missing.extend(shipping_missing + discount_missing + tax_missing)
    landed = taxable + shipping + tax if taxable is not None and shipping is not None and tax is not None and currency == "CNY" and not price_evidence.get("missing") else None
    if _value(sources, "deliverable") is False:
        reasons.append("当前地区不配送")
    if item.get("detail_error"):
        missing.append("详情数据需重新确认")
    status = "unavailable" if reasons else "pending" if missing else "confirmed"
    if reasons:
        landed = None
    target_quantity = amount(target.get("buy_multiple") or target.get("quantity")) or order_quantity
    return {
        "status": status, "label": STATUS_LABELS[status], "missing": list(dict.fromkeys(missing)), "reasons": reasons,
        "cost_status": "confirmed" if landed is not None else "pending", "currency": currency or None,
        "sales_unit": sales_unit or None, "order_quantity": int(order_quantity),
        "merchandise_total": money(merchandise), "landed_total": money(landed), "landed_unit": money(landed / target_quantity) if landed is not None else None,
        "components": {"merchandise": money(merchandise), "shipping": money(shipping), "tax": money(tax), "discount": money(discount)},
        "evidence": {"price": price_evidence, "shipping": shipping_evidence, "tax": tax_evidence, "discount": discount_evidence},
    }
