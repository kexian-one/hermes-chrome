from __future__ import annotations

import asyncio
import copy
import contextvars
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import Future, wait
from contextlib import AsyncExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
import httpx

from ecom_config import EcomConfig, load_ecom_config
from runtime_support import cache_key, read_cache, write_cache, remaining_timeout, cancellable_sleep, refresh_details, OperationCancelled


log = logging.getLogger(__name__)
_task_session = contextvars.ContextVar("ecom_provider_session", default=None)
_http_session = contextvars.ContextVar("ecom_http_session", default=None)
BUSINESS_FIELDS = {
    "price_tiers", "priceRanges", "price_scope", "endQuantity", "maxQuantity", "unitPrice", "salePrice",
    "sales_unit", "unit", "unitName", "priceUnit", "stock", "quantity", "MOQ", "moq", "min_num", "minOrderQuantity", "currency",
    "stock_unit", "moq_unit", "min_order_unit", "price_tax_included", "tax_included", "tax_rate", "tax_amount", "tax_quote", "tax_basis", "tax_scope", "tax_quantity",
    "shipping_quote", "freightInfo", "shipping_rules", "freeShipping", "freeDeliverFee", "post_fee", "shipping_weight_g", "deliverable",
    "discount_quote", "discounts", "barcode", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "upc", "identifiers", "barcode_scope", "identifier_scope", "gtin_scope", "scope", "barcode_level", "gtin_level", "identifier_level", "level",
}


def _business_fields(source: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(source[key]) for key in BUSINESS_FIELDS if key in source}


class DataSourceStats:
    def __init__(self, cost_per_call_yuan: float | None = None) -> None:
        self.cost_per_call_yuan = cost_per_call_yuan
        self.cache_hits: int = 0
        self.coalesced_hits: int = 0
        self.new_calls: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def record(self, operation: str) -> None:
        with self._lock:
            self.new_calls[operation] += 1

    def record_hit(self, *, coalesced: bool = False) -> None:
        with self._lock:
            if coalesced:
                self.coalesced_hits += 1
            else:
                self.cache_hits += 1

    def total_new(self) -> int:
        return sum(self.new_calls.values())

    def total_cost_yuan(self) -> float | None:
        return self.total_new() * self.cost_per_call_yuan if self.cost_per_call_yuan is not None else None

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            estimate = self.total_cost_yuan()
            return {"cache_hits": self.cache_hits, "coalesced_hits": self.coalesced_hits, "new_calls": dict(self.new_calls), "cost_yuan": round(estimate, 4) if estimate is not None else None, "estimated_cost_yuan": round(estimate, 4) if estimate is not None else None, "actual_cost_yuan": None, "billing_source": None}


def _coalesced_request(lock: threading.Lock, pending: dict[str, Future], key: str, load: Callable[[], Any], timeout: float, stats: DataSourceStats) -> Any:
    remaining_timeout(timeout)
    with lock:
        future = pending.get(key)
        owner = future is None
        if owner:
            future = pending[key] = Future()
    if not owner:
        stats.record_hit(coalesced=True)
        while not future.done():
            wait([future], timeout=min(0.05, remaining_timeout(timeout)))
        remaining_timeout(timeout)
        return copy.deepcopy(future.result())
    try:
        result = load()
        remaining_timeout(timeout)
        future.set_result(result)
        return copy.deepcopy(result)
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with lock:
            if pending.get(key) is future:
                pending.pop(key, None)


class OneboundClient:
    def __init__(self, cfg: EcomConfig | None = None, cache_dir: Path | None = None) -> None:
        self.cfg = cfg or load_ecom_config()
        self.key = str(self.cfg.onebound.get("key") or "")
        self.secret = str(self.cfg.onebound.get("secret") or "")
        if not self.key or not self.secret:
            raise RuntimeError("onebound key/secret missing in ecom_best_source.onebound")
        self.base = str(self.cfg.onebound.get("base") or "https://api-gw.onebound.cn/1688").rstrip("/")
        self.timeout = int(self.cfg.onebound.get("http_timeout") or 30)
        self.cache_dir = cache_dir or _cache_dir("onebound")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._request_lock = threading.Lock()
        self._pending_requests: dict[str, Future] = {}
        self._http_client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        fee = self.cfg.onebound.get("cost_per_call_yuan")
        self.stats = DataSourceStats(float(fee) if fee not in (None, "") else None)

    def search(self, q: str, page: int = 1, page_size: int = 100, lang: str = "zh-CN") -> list[dict[str, Any]]:
        data = self._request("item_search", {
            "q": q,
            "page": int(page),
            "page_size": int(page_size),
            "lang": lang,
        })
        items = data.get("items") or {}
        arr = items.get("item") if isinstance(items, dict) else None
        return [normalize_candidate({**x, "provider": "onebound"}, source="text") for x in arr or [] if isinstance(x, dict)]

    def search_image(self, img_url: str, page: int = 1, page_size: int = 50, lang: str = "zh-CN") -> list[dict[str, Any]]:
        clean_url = re.sub(r"\.webp$", "", img_url)
        data = self._request("item_search_img", {
            "imgid": clean_url,
            "page": int(page),
            "page_size": int(page_size),
            "lang": lang,
        })
        items = data.get("items") or {}
        arr = items.get("item") if isinstance(items, dict) else None
        return [normalize_candidate({**x, "provider": "onebound"}, source="image") for x in arr or [] if isinstance(x, dict)]

    def item_get(self, num_iid: str, lang: str = "zh-CN") -> dict[str, Any]:
        data = self._request("item_get", {
            "num_iid": str(num_iid),
            "cache": "no",
            "lang": lang,
        })
        item = data.get("item")
        return _normalize_onebound_detail(item) if isinstance(item, dict) else {}

    def seller_info(self, sid: str, lang: str = "zh-CN") -> dict[str, Any]:
        data = self._request("seller_info", {"sid": str(sid), "lang": lang})
        if isinstance(data.get("seller"), dict):
            return data["seller"]
        return data if isinstance(data, dict) else {}

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        request_key = cache_key(path, [params, bool(refresh_details.get())])
        return _coalesced_request(self._request_lock, self._pending_requests, request_key, lambda: self._request_locked(path, params), self.timeout, self.stats)

    def close(self) -> None:
        self._http_client.close()

    def _request_locked(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        cache_file = self.cache_dir / f"{_cache_key(path, {**params, '_scope': hashlib.sha256((self.base + self.key).encode()).hexdigest()})}.json"
        ttl = float(self.cfg.runtime.get("detail_cache_seconds", 60)) if path == "item_get" else 3600 if path == "seller_info" else 300
        if path == "item_get" and refresh_details.get():
            ttl = 0
        cached = read_cache(cache_file, ttl)
        if cached is not None:
            self.stats.record_hit()
            return cached
        full_params = {**params, "key": self.key, "secret": self.secret}
        url = f"{self.base}/{path}/?{urllib.parse.urlencode(full_params)}"
        data: dict[str, Any] = {}
        last_error = ""
        for attempt, delay in enumerate([0.0, 0.5, 1.5], start=1):
            if delay:
                cancellable_sleep(delay)
            try:
                timeout = remaining_timeout(self.timeout)
                self.stats.record(path)
                http_context = _http_session.set(self._http_client)
                try:
                    data = _http_get_json(url, timeout)
                finally:
                    _http_session.reset(http_context)
                remaining_timeout(self.timeout)
            except Exception as exc:
                remaining_timeout(self.timeout)
                last_error = self._redact(str(exc))
                if attempt == 3:
                    raise RuntimeError(f"onebound {path} request failed: {last_error}") from exc
                continue
            err_code = str(data.get("error_code") or "")
            if not err_code or err_code == "0000":
                break
            msg = self._redact(str(data.get("reason") or data.get("error") or ""))
            if not _onebound_error_retriable(err_code, msg) or attempt == 3:
                raise RuntimeError(f"onebound {path} returned error_code={err_code}: {msg[:200]}")
            last_error = f"error_code={err_code}: {msg[:120]}"
        else:
            raise RuntimeError(f"onebound {path} request failed: {last_error}")
        if isinstance(data.get("item"), dict):
            data["item"]["fetched_at"] = time.time()
            data["item"]["provider"] = "onebound"
        remaining_timeout(self.timeout)
        write_cache(cache_file, data)
        return data

    def _redact(self, text: str) -> str:
        return str(text).replace(self.key, "***OB_KEY***").replace(self.secret, "***OB_SECRET***")


def _normalize_onebound_detail(item: dict[str, Any]) -> dict[str, Any]:
    from product_match import sku_rows
    detail = copy.deepcopy(item)
    images = detail.get("prop_imgs") or []
    if isinstance(images, dict):
        images = images.get("prop_img") or []
    images = images if isinstance(images, list) else []
    for row in sku_rows(detail):
        bound_urls = _sku_image_urls(row)
        if bound_urls:
            row["sku_image_url"] = bound_urls[0]
            row["sku_image_urls"] = bound_urls
            continue
        properties = set(str(row.get("properties") or "").split(";")) - {""}
        bound_images = {str(img.get("url") or img.get("image") or "") for img in images if isinstance(img, dict) and str(img.get("properties") or "") in properties}
        bound_images.discard("")
        if len(bound_images) == 1:
            row["sku_image_url"] = bound_images.pop()
            row["sku_image_urls"] = [row["sku_image_url"]]
    return detail


def _sku_image_urls(row: dict[str, Any]) -> list[str]:
    values = []
    for key in ("sku_image_url", "skuImageUrl", "image", "pic_url", "sku_image_urls", "skuImageUrls", "image_urls", "imageUrls"):
        value = row.get(key)
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, dict):
                entry = entry.get("url") or entry.get("imageUrl")
            if isinstance(entry, str) and entry.strip():
                values.append(entry.strip())
    return list(dict.fromkeys(values))


class AlphashopMCPClient:
    TOOL_KEYWORD = "keywordSearchProduct"
    TOOL_IMAGE = "imageSearchProduct"
    TOOL_DETAIL = "productDetailQuery"

    def __init__(self, cfg: EcomConfig | None = None, cache_dir: Path | None = None) -> None:
        self.cfg = cfg or load_ecom_config()
        mcp_cfg = self.cfg.alphashop_mcp
        self.ak = str(mcp_cfg.get("ak") or "")
        self.sk = str(mcp_cfg.get("sk") or "")
        if not self.ak or not self.sk:
            raise RuntimeError("alphashop ak/sk missing in ecom_best_source.alphashop_mcp")
        self.endpoint = str(mcp_cfg.get("endpoint") or "https://mcp.alphashop.cn/sse")
        self.jwt_expire_seconds = int(mcp_cfg.get("jwt_expire_seconds") or 1800)
        self.timeout = int(mcp_cfg.get("http_timeout") or 60)
        self.cache_dir = cache_dir or _cache_dir("alphashop")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._request_lock = threading.Lock()
        self._pending_requests: dict[str, Future] = {}
        self.stats = DataSourceStats(mcp_cfg.get("cost_per_call_yuan"))
        self._jwt_cache: tuple[str, float] | None = None
        self._runner = _MCPSessionRunner(self._build_url, self.timeout)

    def search(self, q: str, page: int = 1, page_size: int = 100, lang: str = "zh-CN") -> list[dict[str, Any]]:
        del page_size, lang
        payload = self._call(self.TOOL_KEYWORD, {
            "keyword": str(q),
            "beginPage": _clamp_page(page),
        })
        return [normalize_candidate({**x, "provider": "alphashop"}, source="text") for x in _normalize_mcp_search_response(payload)]

    def search_image(self, img_url: str, page: int = 1, page_size: int = 50, lang: str = "zh-CN") -> list[dict[str, Any]]:
        del page_size, lang
        payload = self._call(self.TOOL_IMAGE, {
            "imgUrl": str(img_url),
            "beginPage": _clamp_page(page),
        })
        return [normalize_candidate({**x, "provider": "alphashop"}, source="image") for x in _normalize_mcp_search_response(payload)]

    def item_get(self, num_iid: str, lang: str = "zh-CN") -> dict[str, Any]:
        del lang
        payload = self._call(self.TOOL_DETAIL, {"productId": str(num_iid)})
        return _normalize_mcp_detail_response(payload)

    def seller_info(self, sid: str, lang: str = "zh-CN") -> dict[str, Any]:
        del sid, lang
        return {}

    def close(self) -> None:
        self._runner.close()

    def _call(self, tool: str, params: dict[str, Any]) -> Any:
        request_key = cache_key(tool, [params, bool(refresh_details.get())])
        return _coalesced_request(self._request_lock, self._pending_requests, request_key, lambda: self._call_locked(tool, params), self.timeout, self.stats)

    def _call_locked(self, tool: str, params: dict[str, Any]) -> Any:
        cache_file = self.cache_dir / f"{_cache_key(tool, {**params, '_scope': hashlib.sha256((self.endpoint + self.ak).encode()).hexdigest()})}.json"
        ttl = float(self.cfg.runtime.get("detail_cache_seconds", 60)) if tool == self.TOOL_DETAIL else 300
        if tool == self.TOOL_DETAIL and refresh_details.get():
            ttl = 0
        cached = read_cache(cache_file, ttl)
        if cached is not None:
            self.stats.record_hit()
            return cached
        remaining_timeout(self.timeout)
        self.stats.record(tool)
        payload = self._runner.call_tool(tool, params)
        remaining_timeout(self.timeout)
        if isinstance(payload, dict):
            payload["_fetched_at"] = time.time()
        if _cacheable_payload(payload):
            write_cache(cache_file, payload)
        return payload

    def _jwt_token(self) -> str:
        now = time.time()
        if self._jwt_cache:
            token, exp = self._jwt_cache
            if exp - now > 60:
                return token
        iat = int(now)
        exp = iat + self.jwt_expire_seconds
        payload = {"iss": self.ak, "iat": iat, "nbf": iat - 5, "exp": exp}
        token = _jwt_hs256(payload, self.sk)
        self._jwt_cache = (token, exp)
        return token

    def _build_url(self) -> str:
        sep = "&" if "?" in self.endpoint else "?"
        return f"{self.endpoint}{sep}key={urllib.parse.quote(self._jwt_token(), safe='')}"


class HybridDataClient:
    def __init__(self, cfg: EcomConfig | None = None, *, provider_pool: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or load_ecom_config()
        self.mode = self.cfg.data_source
        self._ob: OneboundClient | None = None
        self._mcp: AlphashopMCPClient | None = None
        self._provider_pool = provider_pool
        self._init_clients()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "onebound": self._ob.stats.to_dict() if self._ob else None,
            "alphashop_mcp": self._mcp.stats.to_dict() if self._mcp else None,
        }

    def search(self, q: str, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        return self._call_with_fallback("search", q, page=page, page_size=page_size)

    def search_image(self, img_url: str, page: int = 1, page_size: int = 50) -> list[dict[str, Any]]:
        return self._call_with_fallback("search_image", img_url, page=page, page_size=page_size)

    def item_get(self, num_iid: str) -> dict[str, Any]:
        if self.mode == "hybrid" and self._mcp and self._ob:
            detail: dict[str, Any] = {}
            try:
                detail = self._mcp.item_get(num_iid)
            except Exception as exc:
                remaining_timeout(1)
                log.warning("MCP item_get failed; fallback to onebound: %s", type(exc).__name__)
                return self._ob.item_get(num_iid)
            if _detail_needs_onebound_enrichment(detail):
                try:
                    onebound_detail = self._ob.item_get(num_iid)
                except Exception as exc:
                    remaining_timeout(1)
                    log.warning("onebound item_get enrichment failed: %s", type(exc).__name__)
                else:
                    detail = _merge_detail_enrichment(detail, onebound_detail)
            return detail
        return self._call_with_fallback("item_get", num_iid)

    def seller_info(self, sid: str) -> dict[str, Any]:
        if not self._ob:
            return {}
        return self._ob.seller_info(sid)

    def close(self) -> None:
        if self._provider_pool is not None:
            return
        if self._mcp:
            self._mcp.close()
        if self._ob:
            self._ob.close()

    def _init_clients(self) -> None:
        if self.mode in ("onebound", "hybrid"):
            try:
                self._ob = self._provider_pool.get("onebound") if self._provider_pool is not None else None
                if self._ob is None:
                    self._ob = OneboundClient(self.cfg)
                    if self._provider_pool is not None:
                        self._provider_pool["onebound"] = self._ob
            except Exception as exc:
                if self.mode == "onebound":
                    raise
                log.warning("onebound unavailable in hybrid mode: %s", exc)
        if self.mode in ("mcp", "hybrid"):
            try:
                self._mcp = self._provider_pool.get("alphashop_mcp") if self._provider_pool is not None else None
                if self._mcp is None:
                    self._mcp = AlphashopMCPClient(self.cfg)
                    if self._provider_pool is not None:
                        self._provider_pool["alphashop_mcp"] = self._mcp
            except Exception as exc:
                remaining_timeout(1)
                if self.mode == "mcp":
                    raise
                log.warning("alphashop MCP unavailable in hybrid mode: %s", exc)
        if not self._ob and not self._mcp:
            raise RuntimeError("no ecom data source configured")

    def _call_with_fallback(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._mcp:
            try:
                result = getattr(self._mcp, method)(*args, **kwargs)
                if result or self.mode != "hybrid" or not self._ob:
                    return result
            except Exception as exc:
                remaining_timeout(1)
                if self.mode != "hybrid" or not self._ob:
                    raise
                log.warning("MCP %s failed; fallback to onebound: %s", method, type(exc).__name__)
        if not self._ob:
            return [] if method.startswith("search") else {}
        return getattr(self._ob, method)(*args, **kwargs)


class TaskDataSession:
    def __init__(self, cfg: EcomConfig) -> None:
        self.cfg = cfg
        self.providers: dict[str, Any] = {}
        self.clients: dict[str, HybridDataClient] = {}
        self._lock = threading.Lock()
        self.closed = False

    def client(self, mode: str | None) -> HybridDataClient:
        mode = mode or self.cfg.data_source
        if mode not in {"onebound", "mcp", "hybrid"}:
            raise ValueError("data_source must be onebound, mcp, or hybrid")
        remaining_timeout(1)
        with self._lock:
            if self.closed:
                raise OperationCancelled("任务数据源会话已关闭")
            if mode not in self.clients:
                self.clients[mode] = HybridDataClient(replace(self.cfg, data_source=mode), provider_pool=self.providers)
            return self.clients[mode]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {name: self.providers[name].stats.to_dict() if name in self.providers else None for name in ("onebound", "alphashop_mcp")}

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            providers = list(self.providers.values())
        for provider in providers:
            provider.close()


@contextmanager
def task_data_session(cfg: EcomConfig):
    session = TaskDataSession(cfg)
    token = _task_session.set(session)
    try:
        yield session
    finally:
        _task_session.reset(token)
        session.close()


def release_data_client(client: Any) -> None:
    session = _task_session.get()
    if session is None or not any(value is client for value in session.clients.values()):
        client.close()


def make_data_client(data_source: str | None = None) -> HybridDataClient:
    if os.environ.get("ALL_IN_AI_OFFLINE") == "1":
        raise RuntimeError("offline mode: external sourcing calls disabled")
    session = _task_session.get()
    if session is not None:
        return session.client(data_source)
    cfg = load_ecom_config()
    if data_source:
        if data_source not in {"onebound", "mcp", "hybrid"}:
            raise ValueError("data_source must be onebound, mcp, or hybrid")
        cfg = replace(cfg, data_source=data_source)
    cfg.apply_env()
    return HybridDataClient(cfg)


def normalize_candidate(item: dict[str, Any], source: str) -> dict[str, Any]:
    num_iid = str(
        item.get("num_iid")
        or item.get("offerId")
        or item.get("id")
        or _offer_id_from_url(str(item.get("detail_url") or item.get("detailUrl") or item.get("link") or ""))
        or ""
    )
    detail_url = str(
        item.get("detail_url")
        or item.get("detailUrl")
        or item.get("link")
        or (f"https://detail.1688.com/offer/{num_iid}.html" if num_iid else "")
    )
    out = dict(item)
    out.update({
        "num_iid": num_iid,
        "title": _strip_html(str(item.get("title") or item.get("originTitle") or item.get("aiTitle") or "")),
        "pic_url": str(item.get("pic_url") or item.get("originImageUrl") or item.get("aiImageUrl") or ""),
        "detail_url": detail_url,
        "price": item.get("price") or item.get("promotion_price"),
        "sales": _to_int(item.get("sales") or item.get("soldOut") or item.get("sale")),
        "shopName": _shop_name_from(item),
        "sources": sorted(set([source, *[str(s) for s in item.get("sources", []) if s]])),
    })
    seller = dict(item.get("seller_info") or {})
    seller_id = item.get("sellerId") or item.get("supplierId") or item.get("memberId")
    if seller_id and not seller.get("sid"):
        seller["sid"] = str(seller_id)
    if seller:
        out["seller_info"] = seller
    return out


def merge_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("num_iid") or _offer_id_from_url(str(item.get("detail_url") or "")) or item.get("title") or "")
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(item)
            merged[key]["sources"] = set(item.get("sources") or [])
            continue
        existing = merged[key]
        existing["sources"].update(item.get("sources") or [])
        hits = [*existing.get("retrieval_hits", []), *item.get("retrieval_hits", [])]
        existing["retrieval_hits"] = list({json.dumps(hit, ensure_ascii=False, sort_keys=True): hit for hit in hits}.values())
        for field in ("price", "sales", "pic_url", "detail_url", "shopName"):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]
    out = []
    for item in merged.values():
        item["sources"] = sorted(item["sources"])
        out.append(item)
    return out


def _normalize_mcp_search_response(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize_mcp_search_item(x) for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        raise RuntimeError(f"alphashop payload is not dict/list: {type(payload).__name__}")
    rc = payload.get("resultCode")
    if rc is not None and str(rc).upper() not in ("SUCCESS", "S0000", "OK", ""):
        raise RuntimeError(f"alphashop resultCode={rc}: {payload.get('message') or payload.get('msg') or ''}")
    for key in ("result", "items", "data", "list", "products"):
        value = payload.get(key)
        if isinstance(value, list):
            return [_normalize_mcp_search_item(x) for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            inner = value.get("item") or value.get("list") or value.get("data")
            if isinstance(inner, list):
                return [_normalize_mcp_search_item(x) for x in inner if isinstance(x, dict)]
    return []


def _normalize_mcp_search_item(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        **_business_fields(raw),
        "num_iid": str(raw.get("offerId") or "").strip(),
        "title": str(raw.get("originTitle") or raw.get("aiTitle") or "").strip(),
        "pic_url": str(raw.get("originImageUrl") or raw.get("aiImageUrl") or "").strip(),
        "detail_url": str(raw.get("detailUrl") or "").strip(),
        "price": raw.get("price"),
        "sales": _to_int(raw.get("soldOut")),
        "shopName": _shop_name_from(raw),
        "sellerId": str(raw.get("sellerId") or raw.get("supplierId") or raw.get("memberId") or ""),
    }


def _normalize_mcp_detail_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"alphashop detail payload is not dict: {type(payload).__name__}")
    rc = payload.get("resultCode")
    if rc is not None and str(rc).upper() not in ("SUCCESS", "S0000", "OK", ""):
        raise RuntimeError(f"alphashop detail resultCode={rc}: {payload.get('message') or payload.get('msg') or ''}")
    raw = None
    for key in ("result", "item", "data", "product"):
        if isinstance(payload.get(key), dict):
            raw = payload[key]
            break
    if raw is None:
        return {}
    sku_rows = []
    for sku in raw.get("productSkuInfos") or []:
        if not isinstance(sku, dict):
            continue
        attrs = sku.get("productSkuAttributeInfos") or []
        pairs = []
        sku_images = _sku_image_urls(sku)
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            name = str(attr.get("attributeName") or "").strip()
            value = str(attr.get("value") or "").strip()
            if name and value:
                pairs.append(f"{name}:{value}")
            sku_images.extend(_sku_image_urls(attr))
        sku_images = list(dict.fromkeys(sku_images))
        sku_rows.append({
            **_business_fields(sku),
            "sku_id": str(sku.get("skuId") or ""),
            "properties_name": ";".join(pairs),
            "quantity": _to_int(sku.get("amountOnSale", sku.get("quantity", sku.get("stock")))) if sku.get("amountOnSale", sku.get("quantity", sku.get("stock"))) not in (None, "") else None,
            "price": _to_float(sku.get("price")),
            "sku_image_url": sku_images[0] if sku_images else "",
            "sku_image_urls": sku_images,
            "minOrderQuantity": sku.get("minOrderQuantity"),
            "price_tiers": sku.get("priceRanges") or [],
            "raw": sku,
        })
    props = []
    for prop in raw.get("productAttributeInfos") or []:
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("attributeName") or "").strip()
        value = _normalize_prop_value(name, str(prop.get("value") or "").strip())
        if name:
            props.append({"name": name, "value": value})
    imgs = raw.get("originImageUrls") or raw.get("aiImageUrls") or []
    return {
        **_business_fields(raw),
        "num_iid": str(raw.get("offerId") or "").strip(),
        "title": str(raw.get("originTitle") or raw.get("aiTitle") or "").strip(),
        "pic_url": str(imgs[0]).strip() if isinstance(imgs, list) and imgs else "",
        "item_imgs": [{"url": str(u)} for u in imgs if u] if isinstance(imgs, list) else [],
        "sales": _to_int(raw.get("soldOut")),
        "min_num": _to_int(raw.get("minOrderQuantity")) or 1,
        "unit": str(raw.get("unit") or raw.get("saleUnit") or ""),
        "num": sum(s["quantity"] for s in sku_rows) if sku_rows and all(s["quantity"] is not None for s in sku_rows) else None,
        "price": sku_rows[0].get("price") if len(sku_rows) == 1 else None,
        "price_tiers": raw.get("priceRanges") or [],
        "fetched_at": payload.get("_fetched_at"),
        "provider": "alphashop",
        "raw": raw,
        "skus": {"sku": sku_rows},
        "props": props,
        "seller_info": {
            "sid": str(raw.get("sellerId") or raw.get("supplierId") or raw.get("memberId") or ""),
            "nick": _shop_name_from(raw),
            "star": raw.get("compositeScore") or raw.get("serviceScore"),
            "tpyear": raw.get("shopYear"),
        },
    }


def _detail_needs_onebound_enrichment(detail: dict[str, Any]) -> bool:
    if not detail:
        return True
    seller = detail.get("seller_info") if isinstance(detail.get("seller_info"), dict) else {}
    has_sid = bool(seller.get("sid") or detail.get("sid") or detail.get("seller_id"))
    has_shop = bool(_shop_name_from({"seller_info": seller, **detail}))
    has_score = bool(
        seller.get("star")
        or seller.get("compositeScore")
        or seller.get("serviceScore")
        or _nested(detail, "tradeService", "compositeNewScore")
    )
    has_year = bool(seller.get("tpyear") or seller.get("shopYear") or detail.get("shopYear"))
    from product_match import sku_rows
    rows = sku_rows(detail)
    incomplete_skus = not rows or any(row.get("price") in (None, "") or row.get("quantity") in (None, "") or not row.get("sku_image_url") for row in rows)
    return incomplete_skus or not (has_sid and has_shop and has_score and has_year)


def _merge_detail_enrichment(base: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    if not enrichment:
        return base
    merged = dict(base)
    for key, value in enrichment.items():
        if key == "seller_info":
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    base_seller = base.get("seller_info") if isinstance(base.get("seller_info"), dict) else {}
    enrich_seller = enrichment.get("seller_info") if isinstance(enrichment.get("seller_info"), dict) else {}
    seller = {**base_seller}
    for key, value in enrich_seller.items():
        if seller.get(key) in (None, "") and value not in (None, ""):
            seller[key] = value
    for key in ("sid", "nick", "title", "shopName", "companyName"):
        value = enrichment.get(key)
        if seller.get(key) in (None, "") and value not in (None, ""):
            seller[key] = value
    if seller:
        merged["seller_info"] = seller
    from product_match import sku_rows
    rows, extra_rows = sku_rows(base), sku_rows(enrichment)
    if rows and extra_rows:
        indexed = {str(row.get("sku_id") or row.get("skuId") or ""): row for row in extra_rows}
        enriched_rows = []
        for row in rows:
            result = dict(row)
            row_id = str(row.get("sku_id") or row.get("skuId") or "")
            if row_id and row_id in indexed:
                for key, value in indexed[row_id].items():
                    if result.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                        result[key] = value
            enriched_rows.append(result)
        merged["skus"] = {"sku": enriched_rows}
        timestamps = [float(d["fetched_at"]) for d in (base, enrichment) if d.get("fetched_at")]
        if timestamps:
            merged["fetched_at"] = min(timestamps)
    return merged


class _MCPSessionRunner:
    def __init__(self, url_builder: Callable[[], str], timeout: int) -> None:
        self._url_builder = url_builder
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._ready = threading.Event()
        self._start_err: Exception | None = None
        self._lock = threading.Lock()
        self._stop_event: asyncio.Event | None = None
        self._lifecycle: asyncio.Task | None = None
        self._connect_timeout = timeout

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_started()
        assert self._loop is not None and self._session is not None

        async def _do() -> Any:
            return await self._session.call_tool(name, arguments)

        future = asyncio.run_coroutine_threadsafe(_do(), self._loop)
        try:
            while not future.done():
                wait([future], timeout=min(0.05, remaining_timeout(self._timeout + 10)))
            remaining_timeout(self._timeout + 10)
            return _extract_tool_payload(future.result())
        except (TimeoutError, OperationCancelled):
            future.cancel()
            raise

    def close(self) -> None:
        if self._loop and self._loop.is_running() and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
            if self._thread:
                self._thread.join(timeout=5)
                if self._thread.is_alive() and self._lifecycle:
                    self._loop.call_soon_threadsafe(self._lifecycle.cancel)
                    self._thread.join(timeout=2)

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive() and self._ready.is_set():
            if self._start_err:
                raise RuntimeError(f"MCP session failed to start: {type(self._start_err).__name__}")
            return
        while not self._lock.acquire(timeout=min(0.05, remaining_timeout(self._timeout + 10))):
            pass
        try:
            if self._thread and self._thread.is_alive() and self._ready.is_set():
                if self._start_err:
                    raise RuntimeError(f"MCP session failed to start: {type(self._start_err).__name__}")
                return
            self._ready.clear()
            self._start_err = None
            self._connect_timeout = remaining_timeout(self._timeout)
            self._thread = threading.Thread(target=self._thread_main, name="ecom-alphashop-mcp", daemon=True)
            self._thread.start()
            while not self._ready.wait(timeout=min(0.05, remaining_timeout(self._timeout + 10))):
                pass
            remaining_timeout(self._timeout + 10)
            if self._start_err:
                raise RuntimeError(f"MCP session failed to start: {type(self._start_err).__name__}")
        finally:
            self._lock.release()

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._lifecycle = self._loop.create_task(self._async_lifecycle())
        try:
            self._loop.run_until_complete(self._lifecycle)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._start_err = exc
        finally:
            self._ready.set()
            self._session = None
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _async_lifecycle(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        self._stop_event = asyncio.Event()
        async with AsyncExitStack() as stack:
            self._stack = stack
            read, write = await stack.enter_async_context(sse_client(self._url_builder(), timeout=self._connect_timeout))
            self._session = await stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            self._ready.set()
            await self._stop_event.wait()


def _extract_tool_payload(result: Any) -> Any:
    if result is None:
        return None
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool returned error: {_content_as_text(result)[:300]}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            try:
                return json.loads(text)
            except ValueError:
                continue
    return _content_as_text(result)


def _content_as_text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _jwt_hs256(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join([
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ])
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _cache_dir(name: str) -> Path:
    root = _project_root()
    return root / "outputs" / ".ecom-best-source-cache" / name


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "skills").is_dir():
            return parent
    return Path.cwd()


def _cache_key(path: str, params: dict[str, Any]) -> str:
    return cache_key(path, params)


def _cacheable_payload(payload: Any) -> bool:
    if isinstance(payload, dict) and isinstance(payload.get("result"), list) and not payload["result"]:
        return False
    return True


def _http_get_json(url: str, timeout: float) -> dict[str, Any]:
    session = _http_session.get()
    if session is not None:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    try:
        from curl_cffi import requests as crequests
    except Exception:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    response = crequests.get(url, timeout=timeout, impersonate="chrome120")
    response.raise_for_status()
    return response.json()


def _onebound_error_retriable(error_code: str, message: str) -> bool:
    upper = f"{error_code} {message}".upper()
    return (
        error_code in {"4001", "4002", "5000", "5001", "5002", "5003"}
        or any(token in upper for token in ("BUSY", "TIMEOUT", "LIMIT", "RETRY"))
    )


def _offer_id_from_url(url: str) -> str:
    match = re.search(r"/offer/(\d+)\.html", url)
    return match.group(1) if match else ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _shop_name_from(item: dict[str, Any]) -> str:
    seller = item.get("seller_info") if isinstance(item.get("seller_info"), dict) else {}
    seller_alt = item.get("sellerInfo") if isinstance(item.get("sellerInfo"), dict) else {}
    candidates = [
        item.get("shopName"),
        item.get("shop_name"),
        item.get("storeName"),
        item.get("sellerName"),
        item.get("supplierName"),
        item.get("supplierLoginName"),
        item.get("companyName"),
        item.get("memberName"),
        seller.get("title"),
        seller.get("nick"),
        seller.get("shopName"),
        seller.get("companyName"),
        seller_alt.get("title"),
        seller_alt.get("nick"),
        seller_alt.get("shopName"),
        seller_alt.get("companyName"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _nested(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _to_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"-?\d+", str(value).replace(",", ""))
    return int(match.group(0)) if match else 0


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _clamp_page(page: int) -> int:
    return min(max(int(page or 1), 1), 10)


def _normalize_prop_value(name: str, value: str) -> str:
    if name in {"箱装数量", "装套数量", "整箱数量", "每箱数量"}:
        match = re.match(r"^\s*(\d+)\s*[\*xX×]\s*(\d+)\s*$", value)
        if match:
            return str(int(match.group(1)) * int(match.group(2)))
        digits = re.search(r"\d+", value)
        if digits:
            return digits.group(0)
    return value
