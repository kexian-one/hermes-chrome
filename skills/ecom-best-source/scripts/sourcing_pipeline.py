from __future__ import annotations

import argparse
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from keyword_builder import build_keywords
from sourcing_rules import run_pipeline


CSV_HEADERS = [
    "排名",
    "1688商品标题",
    "价格(元)",
    "起批数",
    "规格匹配",
    "SKU库存",
    "店铺信息",
    "综合服务分",
    "经营年限",
    "得分",
    "推荐理由",
    "风险说明",
    "1688链接",
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
    target_count: int = 5,
) -> dict[str, Any]:
    payload = _load_pipeline_payload(
        jd_product_path=jd_product_path,
        candidates_path=candidates_path,
        merged_input_path=merged_input_path,
        known_brand=known_brand,
        buy_multiple=buy_multiple,
        target_count=target_count,
    )
    result = _run_with_final_detail_confirmation(payload, target_count)
    csv_path = _resolve_output_path(
        output_path or _default_csv_name(result.get("target") or {}),
        csv_visible=True,
    )
    write_csv(result, csv_path)

    if json_output_path:
        json_path = _resolve_output_path(json_output_path, csv_visible=False)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "status": result.get("status"),
        "final_count": len(result.get("final") or []),
        "csv_path": str(csv_path),
        "json_path": str(_resolve_output_path(json_output_path, csv_visible=False)) if json_output_path else "",
        "confirmation": result.get("confirmation") or {},
        "top3": [_summary_item(item) for item in (result.get("final") or [])[:3]],
    }


def _run_with_final_detail_confirmation(
    payload: dict[str, Any],
    target_count: int,
    max_confirmations: int | None = None,
) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return run_pipeline(payload)

    max_confirmations = max_confirmations or max(target_count * 4, 10)
    attempted: set[str] = set()
    confirmed = 0
    errors: list[str] = []
    client = None

    result = run_pipeline(payload)
    while confirmed < max_confirmations:
        final = result.get("final") or []
        to_confirm: list[dict[str, Any]] = []
        for item in final:
            num_iid = str(item.get("num_iid") or "")
            if not num_iid or num_iid in attempted:
                continue
            source = _candidate_by_num_iid(candidates, num_iid)
            if source is not None and _needs_detail_confirmation(source):
                to_confirm.append(source)
        if not to_confirm:
            break

        if client is None:
            try:
                client = _make_data_client()
            except Exception as exc:
                result["confirmation"] = {
                    "enabled": False,
                    "confirmed": confirmed,
                    "error": str(exc)[:200],
                }
                return result

        changed = False
        for candidate in to_confirm:
            num_iid = str(candidate.get("num_iid") or "")
            attempted.add(num_iid)
            try:
                detail = client.item_get(num_iid)
            except Exception as exc:
                errors.append(f"{num_iid}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if isinstance(detail, dict) and detail:
                _merge_confirmed_detail(candidate, detail)
                confirmed += 1
                changed = True
            if confirmed >= max_confirmations:
                break
        if not changed:
            break
        result = run_pipeline(payload)

    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    result["confirmation"] = {
        "enabled": True,
        "confirmed": confirmed,
        "attempted": len(attempted),
        "errors": errors[:5],
    }
    return result


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
    if _detail_sku_rows(detail):
        return False
    return not any(
        detail.get(key) not in (None, "")
        for key in ("num", "stock", "quantity", "min_num", "minOrderQuantity")
    )


def _merge_confirmed_detail(candidate: dict[str, Any], detail: dict[str, Any]) -> None:
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
        candidate.setdefault("seller_info", seller)


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
    kw = build_keywords(title, product_brand)
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
    with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for idx, item in enumerate(result.get("final") or [], start=1):
            writer.writerow(_csv_row(idx, item))


def _csv_row(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    price = _first_number(item, "unitPrice", "unit_price", "price")
    moq = _first_value(item, "MOQ", "moq", "minOrderQuantity")
    service = _first_number(item, "compositeScore", "composite_score", "serviceScore")
    shop_year = _first_value(item, "shopYear", "shop_year")
    score = _first_number(item, "score")
    return {
        "排名": rank,
        "1688商品标题": _safe_cell(str(item.get("title") or "")),
        "价格(元)": _format_number(price),
        "起批数": moq or "",
        "规格匹配": _safe_cell(str(item.get("skuMatchLevel") or "")),
        "SKU库存": _safe_cell(_stock_text(item)),
        "店铺信息": _safe_cell(str(item.get("shopName") or item.get("shop_name") or "")),
        "综合服务分": _format_number(service),
        "经营年限": shop_year or "",
        "得分": _format_number(score),
        "推荐理由": _safe_cell(_recommendation_text(item)),
        "风险说明": _safe_cell(_risk_text(item)),
        "1688链接": _safe_cell(str(item.get("link") or item.get("detail_url") or "")),
    }


def _summary_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(item.get("title") or ""),
        "shop": str(item.get("shopName") or item.get("shop_name") or ""),
        "unit_price": _format_number(_first_number(item, "unitPrice", "unit_price", "price")),
        "moq": _first_value(item, "MOQ", "moq", "minOrderQuantity") or "",
        "score": _format_number(_first_number(item, "score")),
        "stock": _stock_text(item),
        "link": str(item.get("link") or item.get("detail_url") or ""),
        "risk": _risk_text(item),
    }


def _stock_text(item: dict[str, Any]) -> str:
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
    parser.add_argument("--target-count", type=int, default=5)
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
