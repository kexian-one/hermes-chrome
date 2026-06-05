from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_WEIGHTS = {
    "price": 5 / 7,
    "composite_service": 2 / 7,
}

REMOVED_DIMENSION_FIELDS = {
    "repurchaseRate",
    "repurchase_rate",
    "shopRepurchaseRate",
    "responseRate",
    "response_rate",
    "shopResponseRate30d",
    "invoiceSupport",
    "invoice_support",
    "invoiceType",
    "invoice_type",
    "invoiceRate",
    "invoice_rate",
}


@dataclass
class Candidate:
    num_iid: str
    title: str
    link: str
    shop_name: str = ""
    sku_name: str = ""
    sku_match_level: str = ""
    batch_price: float | None = None
    shipping: float | None = None
    unit_price: float | None = None
    jd_price: float | None = None
    price: float | None = None
    moq: int = 0
    composite_score: float | None = None
    shop_year: int = 0
    sources: set[str] = field(default_factory=set)
    detail: dict[str, Any] = field(default_factory=dict)
    seller_info: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    recommendation_level: str = "不推荐"
    rejection: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {
            key: value
            for key, value in self.raw.items()
            if key not in REMOVED_DIMENSION_FIELDS
        }
        data.update({
            "num_iid": self.num_iid,
            "title": self.title,
            "link": self.link,
            "shopName": self.shop_name,
            "skuName": self.sku_name,
            "skuMatchLevel": self.sku_match_level,
            "batchPrice": self.batch_price,
            "shipping": self.shipping,
            "unitPrice": self.unit_price,
            "jdPrice": self.jd_price,
            "price": self.price,
            "MOQ": self.moq,
            "compositeScore": self.composite_score,
            "shopYear": self.shop_year,
            "sources": sorted(self.sources),
            "score": round(self.score, 4),
            "score_breakdown": self.score_breakdown,
            "recommendationLevel": self.recommendation_level,
            "rejection": self.rejection,
            "warnings": self.warnings,
        })
        return data


def run_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("target") or {}
    config = payload.get("config") or {}
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(config.get("weights") or {})
    target_count = int((config.get("output") or {}).get("target_count") or 5)
    candidates = [candidate_from_dict(x, target) for x in payload.get("candidates") or []]

    if not candidates:
        return _result("无供给", target, [], [], target_count)

    score_candidates(candidates, weights)
    final = sorted(candidates, key=lambda c: c.score, reverse=True)[:target_count]

    if not final:
        status = "无供给"
    elif len(final) < target_count:
        status = "召回不足"
    else:
        status = "ok"
    return _result(status, target, final, candidates[target_count:], target_count)


def candidate_from_dict(item: dict[str, Any], target: dict[str, Any]) -> Candidate:
    detail = item.get("detail") or {}
    seller = item.get("seller_info") or item.get("sellerInfo") or {}
    num_iid = str(
        item.get("num_iid")
        or item.get("offerId")
        or item.get("id")
        or _offer_id_from_url(str(item.get("detail_url") or item.get("link") or ""))
        or ""
    )
    link = str(
        item.get("link")
        or item.get("detail_url")
        or (f"https://detail.1688.com/offer/{num_iid}.html" if num_iid else "")
    )
    sources_raw = item.get("sources") or item.get("searchType") or []
    if isinstance(sources_raw, str):
        sources = set(sources_raw.split("+"))
    else:
        sources = {str(s) for s in sources_raw if s}

    c = Candidate(
        num_iid=num_iid,
        title=_strip_html(str(item.get("title") or detail.get("title") or "")),
        link=link,
        shop_name=str(item.get("shopName") or item.get("shop_name") or seller.get("title") or seller.get("nick") or ""),
        sku_name=str(item.get("skuName") or item.get("sku_name") or _best_sku_text(detail) or ""),
        sku_match_level=str(item.get("skuMatchLevel") or item.get("sku_match_level") or ""),
        batch_price=_to_float(item.get("batchPrice") or item.get("batch_price")),
        shipping=_to_float(item.get("shipping")),
        unit_price=_to_float(item.get("unitPrice") or item.get("unit_price") or item.get("price")),
        jd_price=_to_float(item.get("jdPrice") or item.get("jd_price") or target.get("jd_price") or target.get("price")),
        price=_to_float(item.get("price") or item.get("promotion_price")),
        moq=_to_int(item.get("MOQ") or item.get("moq") or item.get("minOrderQuantity") or detail.get("min_num") or detail.get("minOrderQuantity")),
        composite_score=_to_float(
            item.get("compositeScore")
            or item.get("composite_score")
            or item.get("serviceScore")
            or item.get("shop_composite_score")
            or _nested(detail, "tradeService", "compositeNewScore")
            or seller.get("star")
        ),
        shop_year=_to_int(item.get("shopYear") or item.get("shop_year") or seller.get("tpyear") or seller.get("shopYear")),
        sources=sources,
        detail=detail,
        seller_info=seller,
        raw=item,
    )
    if not c.sku_match_level:
        c.sku_match_level = infer_sku_match_level(c, target)
    apply_original_hard_downgrades(c, target)
    return c


def score_candidates(candidates: list[Candidate], weights: dict[str, float]) -> None:
    prices = [c.unit_price for c in candidates if c.unit_price and c.unit_price > 0]
    p_min = min(prices) if prices else 0.0
    p_max = max(prices) if prices else 0.0
    p_range = p_max - p_min if p_max > p_min else 0.0

    for c in candidates:
        price_score = _price_score(c.unit_price, p_max, p_range)
        service_score = _service_score(c.composite_score)
        c.score_breakdown = {
            "price": round(price_score, 2),
            "composite_service": round(service_score, 2),
        }
        c.score = (
            price_score * float(weights.get("price", 0))
            + service_score * float(weights.get("composite_service", 0))
        )
        if c.rejection:
            c.score = 0.0
            c.recommendation_level = "不推荐"
        else:
            c.recommendation_level = recommendation_level(c.score)


def apply_original_hard_downgrades(c: Candidate, target: dict[str, Any]) -> None:
    if c.composite_score is not None and c.composite_score < 3.0:
        c.rejection = "综合服务分 < 3.0"
    if c.shop_year and c.shop_year < 1:
        c.rejection = "入驻年限 < 1 年"
    buy_multiple = _to_int(target.get("buy_multiple") or target.get("batchQuantity") or target.get("purchaseMultiple"))
    if buy_multiple > 0 and c.moq > buy_multiple * 10:
        c.warnings.append("MOQ 过高")


def infer_sku_match_level(c: Candidate, target: dict[str, Any]) -> str:
    target_text = " ".join(str(x) for x in [
        target.get("selected_sku"),
        target.get("skuName"),
        target.get("spec"),
        *(target.get("variant") or []),
    ] if x)
    candidate_text = " ".join([c.title, c.sku_name, _flatten_text(c.detail)])
    if target_text and target_text in candidate_text:
        return "完全一致"
    spec = str(target.get("spec") or "")
    if spec and spec_in_text(spec, candidate_text):
        return "规格同数量不同"
    return "SKU不一致"


def recommendation_level(score: float) -> str:
    if score >= 80:
        return "首选"
    if score >= 70:
        return "次选"
    if score >= 60:
        return "备选"
    return "不推荐"


def spec_in_text(spec: str, text: str) -> bool:
    if not spec or not text:
        return False
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z\u4e00-\u9fff]+)\s*", spec)
    if not match:
        return spec in text
    value, unit = match.group(1), re.escape(match.group(2))
    return re.search(rf"(?<![\d.]){re.escape(value)}\s*{unit}(?![\d.])", text, re.IGNORECASE) is not None


def _price_score(unit_price: float | None, p_max: float, p_range: float) -> float:
    if not unit_price or unit_price <= 0:
        return 0.0
    if p_range <= 0:
        return 100.0
    return max(0.0, min(100.0, (p_max - unit_price) / p_range * 100.0))


def _service_score(composite_score: float | None) -> float:
    if composite_score is None:
        return 0.0
    return max(0.0, min(100.0, composite_score * 20.0))


def _result(
    status: str,
    target: dict[str, Any],
    final: list[Candidate],
    rest: list[Candidate],
    target_count: int,
) -> dict[str, Any]:
    rejected = [c for c in [*final, *rest] if c.rejection]
    return {
        "status": status,
        "target": target,
        "final": [c.to_dict() for c in final],
        "rejected": [c.to_dict() for c in rejected],
        "rejected_reasons": dict(Counter(c.rejection or "unknown" for c in rejected)),
        "stats": {
            "input": len(final) + len(rest),
            "final_count": len(final),
            "target_count": target_count,
        },
        "weights": DEFAULT_WEIGHTS,
    }


def _best_sku_text(detail: dict[str, Any]) -> str:
    rows = _detail_sku_rows(detail)
    if not rows:
        return ""
    row = rows[0]
    return str(row.get("properties_name") or row.get("name") or row.get("skuName") or "")


def _detail_sku_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    skus = detail.get("skus") or detail.get("sku") or {}
    rows = skus.get("sku") or skus.get("list") or [] if isinstance(skus, dict) else skus
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _nested(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


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


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(v) for v in value)
    return str(value)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _offer_id_from_url(url: str) -> str:
    match = re.search(r"/offer/(\d+)\.html", url)
    return match.group(1) if match else ""


def _smoke_payload() -> dict[str, Any]:
    return {
        "target": {
            "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
            "selected_sku": "黑色 75g",
            "jd_price": 4.5,
            "buy_multiple": 40,
            "spec": "75g",
            "variant": ["黑色"],
        },
        "candidates": [
            {
                "num_iid": "1001",
                "title": "红鸟黑色液体鞋油75g",
                "unitPrice": 3.2,
                "compositeScore": 4.8,
                "shopYear": 8,
                "MOQ": 3,
                "skuMatchLevel": "完全一致",
            },
            {
                "num_iid": "1002",
                "title": "红鸟黑色液体鞋油75g",
                "unitPrice": 2.8,
                "compositeScore": 2.5,
                "shopYear": 3,
                "MOQ": 3,
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply original ecom-best-source final scoring rules")
    parser.add_argument("--input", help="Input JSON file")
    parser.add_argument("--output", help="Output JSON file")
    parser.add_argument("--smoke", action="store_true", help="Run built-in smoke sample")
    args = parser.parse_args()
    if args.smoke:
        payload = _smoke_payload()
    elif args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    else:
        parser.error("--input or --smoke is required")
    result = run_pipeline(payload)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
