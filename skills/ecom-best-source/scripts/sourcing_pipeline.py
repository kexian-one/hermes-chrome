from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import time
import hashlib
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Any

from keyword_builder import build_keywords
from sourcing_rules import run_pipeline
from product_match import relevance, target_attributes, sku_rows
from runtime_support import atomic_json, parallel_map, refresh_details, remaining_timeout
from data_sources import release_data_client
from supplier_identity import supplier_key
from sourcing_feedback import export_feedback
from sourcing_costs import STATUS_LABELS


CSV_HEADERS = [
    "1688商品标题",
    "价格(元)",
    "总进货价(元)",
    "利润率",
    "邮费",
    "起批数",
    "规格匹配",
    "SKU库存",
    "店铺信息",
    "综合服务分",
    "经营年限",
    "风险说明",
    "1688链接",
    "匹配状态", "匹配SKU", "SKU ID", "目标规格", "候选规格", "差异原因", "目标图片", "SKU图片", "采购SKU数量", "图片核验",
    "可购状态", "商品金额(元)", "订单运费(元)", "额外税费(元)", "已确认优惠(元)", "到货总成本(元)", "到货单位成本(元)", "销售单位", "收货地区", "成本待确认项", "成本依据", "利润口径",
    "目标条码", "货源条码", "条码核验",
]


def run_from_files(
    *,
    jd_product_path: str | None,
    candidates_path: str | None,
    merged_input_path: str | None,
    output_path: str | None,
    json_output_path: str | None = None,
    known_brand: str | None = None,
    buy_multiple: int | None = None,
    target_count: int = 6,
    confirm_details: bool = True,
) -> dict[str, Any]:
    payload = _load_pipeline_payload(
        jd_product_path=jd_product_path,
        candidates_path=candidates_path,
        merged_input_path=merged_input_path,
        known_brand=known_brand,
        buy_multiple=buy_multiple,
        target_count=target_count,
    )
    result = _run_with_final_detail_confirmation(payload, target_count) if confirm_details else run_pipeline(payload)
    if payload.get("retrieval"):
        result["retrieval"] = payload["retrieval"]
    if payload.get("partial") or result.get("pending") or (result.get("confirmation") or {}).get("errors") or (result.get("confirmation") or {}).get("error"):
        result["status"] = "部分完成"
    csv_path = _resolve_output_path(
        output_path or _default_csv_name(result.get("target") or {}),
        csv_visible=True,
    )
    write_csv(result, csv_path)
    feedback = export_feedback(result, csv_path.with_name(csv_path.stem + "-反馈.csv"))

    if json_output_path:
        json_path = _resolve_output_path(json_output_path, csv_visible=False)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "status": result.get("status"),
        "final_count": len(result.get("final") or []),
        "csv_path": str(csv_path),
        "json_path": str(_resolve_output_path(json_output_path, csv_visible=False)) if json_output_path else "",
        "confirmation": result.get("confirmation") or {},
        "top3": [_summary_item(item) for item in (result.get("final") or [])[:3]],
        "pending_count": len(result.get("pending") or []),
        "schema_version": 2,
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "completed_at": time.time(),
        "retrieval": result.get("retrieval") or {},
        "feedback_path": feedback["feedback_path"],
        "feedback_snapshot_path": feedback["snapshot_path"],
        "confirmed_purchase_count": (result.get("stats") or {}).get("confirmed_purchase_count", 0),
        "pending_purchase_count": (result.get("stats") or {}).get("pending_purchase_count", 0),
    }
    atomic_json(csv_path.with_suffix(".manifest.json"), summary)
    return summary


def _run_with_final_detail_confirmation(payload: dict[str, Any], target_count: int, max_confirmations: int | None = None, *, force_refresh: bool = False) -> dict[str, Any]:
    candidates = payload.get("candidates") or []
    maximum = max_confirmations or max(target_count * 4, 12)
    ranked = sorted(candidates, key=lambda item: relevance(item, payload.get("target") or {}), reverse=True)
    pending = [c for c in ranked if force_refresh or _needs_detail_confirmation(c)][:maximum]
    if not pending:
        return run_pipeline(payload)
    try:
        client = _make_data_client()
    except Exception as exc:
        for candidate in pending:
            candidate["detail_error"] = type(exc).__name__
        result = run_pipeline(payload)
        result["confirmation"] = {"enabled": False, "error": type(exc).__name__, "confirmed": 0}
        return result
    errors = []
    def enrich(offer_id: str) -> tuple[dict, list]:
        detail = copy.deepcopy(client.item_get(offer_id))
        if not detail:
            raise ValueError("empty detail")
        if not detail.get("fetched_at"):
            detail["fetched_at"] = time.time()
        detail_errors = []
        _enrich_detail_seller_info(client, detail, detail_errors)
        return detail, detail_errors
    refresh_context = refresh_details.set(force_refresh)
    try:
        runtime = getattr(getattr(client, "cfg", None), "runtime", {})
        offer_ids = [str(candidate.get("num_iid") or candidate.get("offerId") or "") for candidate in pending]
        outcomes = parallel_map(offer_ids, enrich, concurrency=int(runtime.get("concurrency", 3)), timeout=float(runtime.get("detail_timeout_seconds", 120)))
        for candidate, outcome in zip(pending, outcomes):
            remaining_timeout(1)
            if outcome["ok"]:
                detail, detail_errors = outcome["value"]
                _merge_confirmed_detail(candidate, detail)
                errors.extend(detail_errors)
            else:
                candidate["detail_error"] = outcome["error"]
        errors.extend({"offer_id": c.get("num_iid"), "error": r["error"]} for c, r in zip(pending, outcomes) if not r["ok"])
        result = run_pipeline(payload)
        result["confirmation"] = {"enabled": True, "attempted": len(pending), "confirmed": sum(r["ok"] for r in outcomes), "errors": errors, "api_stats": getattr(client, "stats", {})}
        return result
    finally:
        refresh_details.reset(refresh_context)
        release_data_client(client)


def select_confirmation_batch(candidates: list[dict[str, Any]], target: dict[str, Any], limit: int = 10, per_supplier: int = 2, supplier_attempts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    attempts = supplier_attempts or {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in sorted(candidates, key=lambda item: relevance(item, target), reverse=True):
        groups.setdefault(supplier_key(candidate), []).append(candidate)
    ordered = sorted(groups, key=lambda key: (attempts.get(key, 0), -relevance(groups[key][0], target)))
    selected = []
    for offset in range(max(1, per_supplier)):
        for key in ordered:
            if offset < len(groups[key]):
                selected.append(groups[key][offset])
                if len(selected) >= max(1, limit):
                    return selected
    return selected


def _candidate_by_num_iid(candidates: list[dict[str, Any]], num_iid: str) -> dict[str, Any] | None:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("num_iid") or candidate.get("offerId") or candidate.get("id") or "") == num_iid:
            return candidate
    return None


def _needs_detail_confirmation(candidate: dict[str, Any]) -> bool:
    detail = candidate.get("detail") if isinstance(candidate.get("detail"), dict) else {}
    if not detail:
        return True
    if detail.get("fetched_at") is not None and time.time() - float(detail["fetched_at"]) > 60:
        return True
    if not _has_shop_score_and_year(candidate):
        return True
    if _detail_sku_rows(detail):
        return False
    return not any(
        detail.get(key) not in (None, "")
        for key in ("num", "stock", "quantity", "min_num", "minOrderQuantity")
    )


def _enrich_detail_seller_info(client: Any, detail: dict[str, Any], errors: list[str]) -> None:
    seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    sid = str(
        seller.get("sid")
        or seller.get("seller_id")
        or seller.get("sellerId")
        or detail.get("sid")
        or detail.get("seller_id")
        or ""
    ).strip()
    if not sid or _seller_has_score_and_year(seller):
        return
    try:
        fetched = client.seller_info(sid)
    except Exception as exc:
        errors.append(f"seller_info {sid}: {type(exc).__name__}: {str(exc)[:120]}")
        return
    if isinstance(fetched, dict) and fetched:
        detail["seller_info"] = {**seller, **fetched}


def _seller_has_score_and_year(seller: dict[str, Any]) -> bool:
    score = _first_value(seller, "star", "compositeScore", "composite_score", "serviceScore")
    year = _first_value(seller, "tpyear", "shopYear", "shop_year", "company_time", "years")
    return bool(score and year)


def _has_shop_score_and_year(item: dict[str, Any]) -> bool:
    if not _shop_text(item):
        return False
    return _service_score_value(item) is not None and bool(_shop_year_value(item))


def _service_score_value(item: dict[str, Any]) -> float | None:
    direct = _first_number(item, "compositeScore", "composite_score", "serviceScore")
    if direct is not None:
        return direct
    seller = item.get("seller_info") if isinstance(item.get("seller_info"), dict) else {}
    seller_alt = item.get("sellerInfo") if isinstance(item.get("sellerInfo"), dict) else {}
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    detail_seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    for source in (seller, seller_alt, detail_seller):
        value = _first_number(source, "star", "compositeScore", "composite_score", "serviceScore")
        if value is not None:
            return value
    return None


def _shop_year_value(item: dict[str, Any]) -> Any:
    direct = _first_value(item, "shopYear", "shop_year")
    if direct not in (None, ""):
        return direct
    seller = item.get("seller_info") if isinstance(item.get("seller_info"), dict) else {}
    seller_alt = item.get("sellerInfo") if isinstance(item.get("sellerInfo"), dict) else {}
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    detail_seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    for source in (seller, seller_alt, detail_seller):
        value = _first_value(source, "tpyear", "shopYear", "shop_year", "company_time", "years")
        if value not in (None, ""):
            return value
    return ""


def _merge_confirmed_detail(candidate: dict[str, Any], detail: dict[str, Any]) -> None:
    previous = candidate.get("detail") or {}
    old_rows, new_rows = sku_rows(previous), sku_rows(detail)
    title = detail.get("title") or candidate.get("title")
    for index, row in enumerate(new_rows):
        row_id = str(row.get("sku_id") or row.get("skuId") or "")
        old = next((r for r in old_rows if row_id and str(r.get("sku_id") or r.get("skuId") or "") == row_id), None)
        if old is None and len(new_rows) == len(old_rows) == 1:
            old = old_rows[0]
        if old is None:
            continue
        # Price and inventory may change without invalidating image evidence.
        def identity(r: dict, props: object, product_title: object) -> str:
            api_identifiers = [entry for entry in (r.get("identifiers") or []) if isinstance(entry, dict) and not str(entry.get("source") or "").startswith("image:")]
            fields = {"sku_id", "skuId", "properties_name", "name", "skuName", "sku_image_url", "sku_image_urls", "image_urls", "image", "pic_url", "attributes", "barcode", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "upc", "barcode_level", "gtin_level", "identifier_level", "barcode_scope", "gtin_scope", "identifier_scope"}
            return json.dumps([product_title, props, {k: v for k, v in r.items() if k in fields}, api_identifiers], sort_keys=True, ensure_ascii=False)
        if identity(old, previous.get("props"), candidate.get("title")) == identity(row, detail.get("props"), title):
            for key in ("vision", "vision_attributes"):
                if key in old:
                    row[key] = old[key]
            image_identifiers = [entry for entry in (old.get("identifiers") or []) if isinstance(entry, dict) and str(entry.get("source") or "").startswith("image:")]
            if image_identifiers:
                entries = [*(row.get("identifiers") or []), *image_identifiers]
                row["identifiers"] = list({json.dumps(entry, sort_keys=True, ensure_ascii=False): entry for entry in entries}.values())
    candidate.pop("detail_error", None)
    if title:
        candidate["title"] = title
    candidate["detail"] = detail
    for src_key, dst_key in (
        ("min_num", "MOQ"),
        ("minOrderQuantity", "MOQ"),
        ("price", "unitPrice"),
    ):
        if detail.get(src_key) not in (None, ""):
            candidate[dst_key] = detail[src_key]
    seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    if seller:
        existing = candidate.get("seller_info") if isinstance(candidate.get("seller_info"), dict) else {}
        candidate["seller_info"] = {**existing, **seller}
    shop = _shop_text({"detail": detail, "seller_info": seller})
    if shop and not _shop_text(candidate):
        candidate["shopName"] = shop


def _make_data_client():
    from data_sources import make_data_client
    return make_data_client()


def _load_pipeline_payload(
    *,
    jd_product_path: str | None,
    candidates_path: str | None,
    merged_input_path: str | None,
    known_brand: str | None,
    buy_multiple: int | None,
    target_count: int,
) -> dict[str, Any]:
    if merged_input_path:
        payload = _read_json(_resolve_input_path(merged_input_path))
        payload.setdefault("config", {}).setdefault("output", {})["target_count"] = target_count
        return payload

    if not jd_product_path or not candidates_path:
        raise ValueError("--jd-product and --candidates are required unless --input is provided")

    product = _read_json(_resolve_input_path(jd_product_path))
    fetched = _read_json(_resolve_input_path(candidates_path))
    candidates = fetched.get("candidates") if isinstance(fetched, dict) else fetched
    if not isinstance(candidates, list):
        raise ValueError("candidates input must be a list or an object with a candidates list")

    title = str(product.get("title") or "").strip()
    if not title:
        raise ValueError("JD product input is missing title")

    product_brand = known_brand or _string_or_none(product.get("brand"))
    kw = build_keywords(title + " " + str(product.get("selected_sku") or ""), product_brand)
    image_urls = _image_urls(product)
    target = kw.to_target(
        title,
        jd_url=product.get("jd_url") or product.get("url") or "",
        item_id=product.get("item_id") or "",
        main_image_url=product.get("main_image_url") or (image_urls[0] if image_urls else ""),
        image_urls=image_urls,
        selected_sku=product.get("selected_sku") or product.get("skuName") or product.get("sku_name") or "",
        jd_price=product.get("jd_price") or product.get("price") or "",
    )
    for key in ("attributes", "user_attributes", "allow_pack_substitution", "require_vision", "target_errors", "unconfirmed_attributes", "vision", "destination", "jd_price_tax_included"):
        if key in product:
            target[key] = product[key]
    target["attributes"] = target_attributes(target)
    if buy_multiple is not None:
        target["buy_multiple"] = buy_multiple
    elif product.get("buy_multiple") is not None:
        target["buy_multiple"] = product.get("buy_multiple")

    return {
        "target": target,
        "candidates": candidates,
        "config": {"output": {"target_count": target_count}},
    }


def write_csv(result: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    target = result.get("target") if isinstance(result.get("target"), dict) else {}
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writeheader()
        row_num = 1
        for idx, item in enumerate(result.get("final") or [], start=1):
            writer.writerow(_csv_row(idx, item, target))
            row_num += 1
        while row_num < 8:
            writer.writerow({})
            row_num += 1
        writer.writerow({CSV_HEADERS[1]: _safe_cell(_b9_summary_text(result))})
        for idx, item in enumerate((result.get("pending") or [])[:6]):
            writer.writerow(_csv_row(idx, item, target))


def _csv_row(rank: int, item: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    del rank
    price = _first_number(item, "unitPrice", "unit_price", "price")
    quantity = _target_quantity(target)
    selected = item.get("selected_sku") or {}
    purchase = item.get("purchase") or selected.get("purchase") or {}
    components = purchase.get("components") or {}
    purchase_total = purchase.get("landed_total")
    jd_price = _first_number(target, "jd_price", "jdPrice", "price")
    jd_total = jd_price * quantity if jd_price is not None and quantity > 0 else None
    profit_rate = _profit_rate(jd_total, purchase_total) if target.get("jd_price_tax_included") is True else None
    moq = selected.get("moq") if selected else _first_value(item, "MOQ", "moq", "minOrderQuantity")
    service = _service_score_value(item)
    shop_year = _shop_year_value(item)
    return {
        "1688商品标题": _safe_cell(str(item.get("title") or "")),
        "价格(元)": _format_number(price),
        "总进货价(元)": _format_number(purchase_total),
        "利润率": _format_percent(profit_rate),
        "邮费": _safe_cell(_shipping_text(item)),
        "起批数": moq or "",
        "规格匹配": _safe_cell(str(item.get("skuMatchLevel") or "")),
        "SKU库存": _safe_cell(_stock_text(item)),
        "店铺信息": _safe_cell(_shop_text(item)),
        "综合服务分": _format_number(service),
        "经营年限": _format_number(_to_float(shop_year)),
        "风险说明": _safe_cell(_risk_text(item)),
        "1688链接": _safe_cell(str(item.get("link") or item.get("detail_url") or "")),
        "匹配状态": "合格" if not item.get("rejection") else "待确认",
        "匹配SKU": _safe_cell(str(selected.get("sku_name") or "")),
        "SKU ID": _safe_cell(str(selected.get("sku_id") or "")),
        "目标规格": _safe_cell(json.dumps(target_attributes(target), ensure_ascii=False)),
        "候选规格": _safe_cell(json.dumps(selected.get("attributes") or {}, ensure_ascii=False)),
        "差异原因": _safe_cell(json.dumps(item.get("match_evidence") or {}, ensure_ascii=False)),
        "目标图片": _safe_cell(str(target.get("main_image_url") or "")),
        "SKU图片": _safe_cell(str(selected.get("sku_image_url") or "")),
        "采购SKU数量": selected.get("order_quantity", ""),
        "图片核验": _safe_cell(str((selected.get("vision") or {}).get("status") or "未核验")),
        "可购状态": STATUS_LABELS.get(purchase.get("status"), "可购性待确认"),
        "商品金额(元)": _format_number(components.get("merchandise")),
        "订单运费(元)": _format_number(components.get("shipping")),
        "额外税费(元)": _format_number(components.get("tax")),
        "已确认优惠(元)": _format_number(components.get("discount")),
        "到货总成本(元)": _format_number(purchase_total),
        "到货单位成本(元)": _format_number(purchase.get("landed_unit")),
        "销售单位": _safe_cell(str(purchase.get("sales_unit") or "待确认")),
        "收货地区": _safe_cell(json.dumps(target.get("destination"), ensure_ascii=False) if target.get("destination") else "待确认"),
        "成本待确认项": _safe_cell("；".join(purchase.get("missing") or [])),
        "成本依据": _safe_cell(json.dumps(purchase.get("evidence") or {}, ensure_ascii=False)),
        "利润口径": "京东含税参考售价与到货采购成本差额；未含销售平台费用" if profit_rate is not None else "成本或京东售价含税口径待确认",
        "目标条码": _safe_cell(json.dumps(selected.get("target_identifiers") or [], ensure_ascii=False)),
        "货源条码": _safe_cell(json.dumps(selected.get("identifiers") or [], ensure_ascii=False)),
        "条码核验": {"matched": "同层级一致", "conflict": "冲突待核实"}.get((selected.get("identifier_comparison") or {}).get("status"), "未确认"),
    }


def _target_quantity(target: dict[str, Any]) -> int:
    return _to_int(
        target.get("buy_multiple")
        or target.get("quantity")
        or target.get("qty")
        or target.get("batchQuantity")
        or target.get("purchaseMultiple")
    )


def _profit_rate(jd_total: float | None, purchase_total: float | None) -> float | None:
    if jd_total is None or jd_total <= 0 or purchase_total is None:
        return None
    return (jd_total - purchase_total) / jd_total * 100


def _summary_item(item: dict[str, Any]) -> dict[str, Any]:
    purchase = item.get("purchase") or {}
    return {
        "title": str(item.get("title") or ""),
        "shop": _shop_text(item),
        "unit_price": _format_number(_first_number(item, "unitPrice", "unit_price", "price")),
        "moq": _first_value(item, "MOQ", "moq", "minOrderQuantity") or "",
        "score": _format_number(_first_number(item, "score")),
        "stock": _stock_text(item),
        "link": str(item.get("link") or item.get("detail_url") or ""),
        "risk": _risk_text(item),
        "purchase_status": purchase.get("status", "pending"),
        "purchase_label": STATUS_LABELS.get(purchase.get("status"), "可购性待确认"),
        "landed_total": purchase.get("landed_total"),
        "landed_unit": purchase.get("landed_unit"),
        "cost_pending": purchase.get("missing") or [],
    }


def _b9_summary_text(result: dict[str, Any]) -> str:
    final = result.get("final") or []
    if not isinstance(final, list) or not final:
        return ""
    first = final[0] if isinstance(final[0], dict) else {}
    first_link = str(first.get("link") or first.get("detail_url") or "").strip()
    shops: list[str] = []
    seen: set[str] = set()
    for item in final:
        if not isinstance(item, dict):
            continue
        shop = _shop_text(item).strip()
        if shop and shop not in seen:
            seen.add(shop)
            shops.append(shop)
    return "，".join([part for part in [first_link, *shops] if part])


def _shop_text(item: dict[str, Any]) -> str:
    candidates = [
        item.get("shopName"),
        item.get("shop_name"),
        item.get("storeName"),
        item.get("sellerName"),
        item.get("supplierName"),
        item.get("companyName"),
        item.get("memberName"),
    ]
    seller = item.get("seller_info") if isinstance(item.get("seller_info"), dict) else {}
    seller_alt = item.get("sellerInfo") if isinstance(item.get("sellerInfo"), dict) else {}
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    detail_seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    for source in (seller, seller_alt, detail_seller):
        candidates.extend([
            source.get("title"),
            source.get("nick"),
            source.get("shopName"),
            source.get("shop_name"),
            source.get("companyName"),
            source.get("memberName"),
        ])
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _shipping_text(item: dict[str, Any]) -> str:
    purchase = item.get("purchase")
    if isinstance(purchase, dict):
        shipping = (purchase.get("components") or {}).get("shipping")
        if shipping is not None:
            return "包邮" if shipping == 0 else _format_number(shipping)
        return "；".join(text for text in purchase.get("missing") or [] if any(key in text for key in ("运费", "免邮", "地区"))) or "订单运费待确认"
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    freight = detail.get("freightInfo") if isinstance(detail.get("freightInfo"), dict) else {}
    free_signals = [
        item.get("freeShipping"),
        item.get("free_shipping"),
        item.get("postFree"),
        item.get("freeDeliverFee"),
        item.get("free_deliver_fee"),
        detail.get("postFree"),
        detail.get("freeDeliverFee"),
        detail.get("marketingFreePostage"),
        freight.get("freeDeliverFee"),
        freight.get("marketingFreePostage"),
    ]
    if any(_truthy_shipping_free(value) for value in free_signals):
        return "包邮"

    for key in ("shipping", "shipping_fee", "freight", "freight_fee", "delivery_fee"):
        value = item.get(key)
        if value not in (None, ""):
            amount = _first_number({"value": value}, "value")
            if amount == 0:
                return "包邮"
            return _format_number(amount) if amount is not None else str(value)

    for key in ("shipping_fee", "freight", "freight_fee", "delivery_fee"):
        value = detail.get(key)
        if value not in (None, ""):
            amount = _first_number({"value": value}, "value")
            if amount == 0:
                return "包邮"
            return _format_number(amount) if amount is not None else str(value)

    total_cost = _first_number(freight, "totalCost")
    if total_cost is not None:
        return "包邮" if total_cost == 0 else _format_number(total_cost)

    first_fee = _first_number(detail, "post_fee", "express_fee", "postFeeValue")
    if first_fee is None:
        first_fee = _first_number(item, "post_fee", "express_fee", "postFeeValue")
    if first_fee is not None:
        return "包邮" if first_fee == 0 else f"首费{_format_number(first_fee)}"
    return "待确认"


def _truthy_shipping_free(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "包邮", "免邮", "free"}


def _stock_text(item: dict[str, Any]) -> str:
    selected = item.get("selected_sku")
    if isinstance(selected, dict):
        return f"匹配SKU库存 {selected['stock']}" if selected.get("stock") is not None else "待确认"
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    sku_rows = _detail_sku_rows(detail)
    sku_name = str(item.get("skuName") or item.get("sku_name") or "")
    if sku_rows:
        matched = _matching_sku_rows(sku_rows, sku_name)
        rows = matched or sku_rows
        quantities = [_to_int(row.get("quantity") or row.get("amountOnSale") or row.get("num")) for row in rows]
        quantities = [q for q in quantities if q > 0]
        if quantities:
            label = "匹配SKU库存" if matched else "SKU库存"
            return f"{label} {sum(quantities)}"
    total = _to_int(detail.get("num") or detail.get("stock") or detail.get("quantity"))
    if total > 0:
        return f"总库存 {total}"
    return "待确认"


def _detail_sku_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    skus = detail.get("skus") or detail.get("sku") or {}
    rows = skus.get("sku") or skus.get("list") or [] if isinstance(skus, dict) else skus
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _matching_sku_rows(rows: list[dict[str, Any]], sku_name: str) -> list[dict[str, Any]]:
    if not sku_name:
        return []
    needles = [part.strip() for part in re.split(r"[;,\s/]+", sku_name) if part.strip()]
    if not needles:
        return []
    out = []
    for row in rows:
        text = " ".join(str(row.get(k) or "") for k in ("properties_name", "name", "skuName"))
        if all(needle in text for needle in needles):
            out.append(row)
    return out


def _recommendation_text(item: dict[str, Any]) -> str:
    level = str(item.get("recommendationLevel") or "")
    score = _format_number(_first_number(item, "score"))
    sku = str(item.get("skuMatchLevel") or "SKU待确认")
    price = _format_number(_first_number(item, "unitPrice", "unit_price", "price"))
    sources = item.get("sources") or []
    source_text = "+".join(str(s) for s in sources) if isinstance(sources, list) else str(sources or "")
    parts = [p for p in [level, f"得分{score}" if score else "", f"单价{price}" if price else "", sku, source_text] if p]
    return "；".join(parts)


def _risk_text(item: dict[str, Any]) -> str:
    risks: list[str] = []
    rejection = item.get("rejection")
    if rejection:
        risks.append(str(rejection))
    warnings = item.get("warnings") or []
    if isinstance(warnings, list):
        risks.extend(str(w) for w in warnings if w)
    sku = str(item.get("skuMatchLevel") or "")
    if sku in {"SKU不一致", "不一致"}:
        risks.append("SKU需人工复核")
    if _stock_text(item) == "待确认":
        risks.append("库存待确认")
    return "；".join(dict.fromkeys(risks)) or "无明显硬性风险"


def _image_urls(product: dict[str, Any]) -> list[str]:
    raw = product.get("image_urls") or []
    if not isinstance(raw, list):
        raw = []
    urls = [str(u).strip() for u in raw if str(u).strip()]
    main = str(product.get("main_image_url") or "").strip()
    if main and main not in urls:
        urls.insert(0, main)
    return urls


def _resolve_input_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path

    project_root = _project_root()
    output_dir = _output_dir()
    candidates = [
        _scratch_dir(),
        output_dir,
        project_root,
    ]
    for base in candidates:
        if base is None:
            continue
        candidate = (base / value).resolve()
        if candidate.is_file():
            return candidate
    return (project_root / value).resolve()


def _resolve_output_path(value: str, *, csv_visible: bool) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if csv_visible:
        base = _output_dir() or _project_root()
    else:
        base = _scratch_dir()
    base.mkdir(parents=True, exist_ok=True)
    return (base / path.name).resolve()


def _project_root() -> Path:
    raw = os.environ.get("WORKER_PROJECT_ROOT", "").strip()
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def _output_dir() -> Path | None:
    raw = os.environ.get("WORKER_OUTPUT_DIR", "").strip()
    return Path(raw).resolve() if raw else None


def _scratch_dir() -> Path:
    output_dir = _output_dir()
    if output_dir:
        return output_dir / ".ecom-scratch"
    return _project_root() / "outputs" / ".ecom-scratch"


def _default_csv_name(target: dict[str, Any]) -> str:
    title = str(target.get("title") or "商品")
    brand = str(target.get("brand") or "")
    category = str(target.get("category") or "")
    spec = str(target.get("spec") or "")
    short = "".join([brand, category, spec]).strip() or title
    short = re.sub(r"[^\w\u4e00-\u9fff]+", "", short)[:24] or "商品"
    return f"找货_{short}_{datetime.now().strftime('%Y%m%d')}.csv"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _first_number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _to_float(item.get(key))
        if number is not None:
            return number
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d+", str(value).replace(",", ""))
    return int(match.group(0)) if match else 0


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_percent(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}%"


def _safe_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Build final ecom-best-source CSV from JD product and 1688 candidates")
    parser.add_argument("--jd-product", help="JD product JSON from jd_product.py")
    parser.add_argument("--candidates", help="Candidate JSON from fetch_candidates.py")
    parser.add_argument("--input", help="Merged sourcing_rules.py input JSON")
    parser.add_argument("--output", help="Final CSV path. Defaults to 找货_<商品>_<YYYYMMDD>.csv")
    parser.add_argument("--json-output", help="Optional debug JSON path; kept in scratch when run by worker")
    parser.add_argument("--known-brand")
    parser.add_argument("--buy-multiple", type=int)
    parser.add_argument("--target-count", type=int, default=6)
    args = parser.parse_args()

    try:
        summary = run_from_files(
            jd_product_path=args.jd_product,
            candidates_path=args.candidates,
            merged_input_path=args.input,
            output_path=args.output,
            json_output_path=args.json_output,
            known_brand=args.known_brand,
            buy_multiple=args.buy_multiple,
            target_count=args.target_count,
        )
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
