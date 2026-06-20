from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ecom_config import EcomConfig, load_ecom_config


log = logging.getLogger(__name__)


class DataSourceStats:
    def __init__(self, cost_per_call_yuan: float = 0.0) -> None:
        self.cost_per_call_yuan = cost_per_call_yuan
        self.cache_hits: int = 0
        self.new_calls: dict[str, int] = defaultdict(int)

    def total_new(self) -> int:
        return sum(self.new_calls.values())

    def total_cost_yuan(self) -> float:
        return self.total_new() * self.cost_per_call_yuan

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_hits": self.cache_hits,
            "new_calls": dict(self.new_calls),
            "cost_yuan": round(self.total_cost_yuan(), 4),
        }


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
        self.stats = DataSourceStats(float(self.cfg.onebound.get("cost_per_call_yuan") or 0.022))

    def search(self, q: str, page: int = 1, page_size: int = 100, lang: str = "zh-CN") -> list[dict[str, Any]]:
        data = self._request("item_search", {
            "q": q,
            "page": int(page),
            "page_size": int(page_size),
            "lang": lang,
        })
        items = data.get("items") or {}
        arr = items.get("item") if isinstance(items, dict) else None
        return [normalize_candidate(x, source="text") for x in arr or [] if isinstance(x, dict)]

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
        return [normalize_candidate(x, source="image") for x in arr or [] if isinstance(x, dict)]

    def item_get(self, num_iid: str, lang: str = "zh-CN") -> dict[str, Any]:
        data = self._request("item_get", {
            "num_iid": str(num_iid),
            "cache": "no",
            "lang": lang,
        })
        item = data.get("item")
        return item if isinstance(item, dict) else {}

    def seller_info(self, sid: str, lang: str = "zh-CN") -> dict[str, Any]:
        data = self._request("seller_info", {"sid": str(sid), "lang": lang})
        if isinstance(data.get("seller"), dict):
            return data["seller"]
        return data if isinstance(data, dict) else {}

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        cache_file = self.cache_dir / f"{_cache_key(path, params)}.json"
        if cache_file.is_file():
            self.stats.cache_hits += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))
        self.stats.new_calls[path] += 1
        full_params = {**params, "key": self.key, "secret": self.secret}
        url = f"{self.base}/{path}/?{urllib.parse.urlencode(full_params)}"
        data: dict[str, Any] = {}
        last_error = ""
        for attempt, delay in enumerate([0.0, 0.5, 1.5], start=1):
            if delay:
                time.sleep(delay)
            try:
                data = _http_get_json(url, self.timeout)
            except Exception as exc:
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
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def _redact(self, text: str) -> str:
        return str(text).replace(self.key, "***OB_KEY***").replace(self.secret, "***OB_SECRET***")


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
        self.stats = DataSourceStats(0.0)
        self._jwt_cache: tuple[str, float] | None = None
        self._runner = _MCPSessionRunner(self._build_url, self.timeout)

    def search(self, q: str, page: int = 1, page_size: int = 100, lang: str = "zh-CN") -> list[dict[str, Any]]:
        del page_size, lang
        payload = self._call(self.TOOL_KEYWORD, {
            "keyword": str(q),
            "beginPage": _clamp_page(page),
        })
        return [normalize_candidate(x, source="text") for x in _normalize_mcp_search_response(payload)]

    def search_image(self, img_url: str, page: int = 1, page_size: int = 50, lang: str = "zh-CN") -> list[dict[str, Any]]:
        del page_size, lang
        payload = self._call(self.TOOL_IMAGE, {
            "imgUrl": str(img_url),
            "beginPage": _clamp_page(page),
        })
        return [normalize_candidate(x, source="image") for x in _normalize_mcp_search_response(payload)]

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
        cache_file = self.cache_dir / f"{_cache_key(tool, params)}.json"
        if cache_file.is_file():
            self.stats.cache_hits += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))
        self.stats.new_calls[tool] += 1
        payload = self._runner.call_tool(tool, params)
        if _cacheable_payload(payload):
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
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
    def __init__(self, cfg: EcomConfig | None = None) -> None:
        self.cfg = cfg or load_ecom_config()
        self.mode = self.cfg.data_source
        self._ob: OneboundClient | None = None
        self._mcp: AlphashopMCPClient | None = None
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
                log.warning("MCP item_get failed; fallback to onebound: %s", exc)
                return self._ob.item_get(num_iid)
            if _detail_needs_onebound_enrichment(detail):
                try:
                    onebound_detail = self._ob.item_get(num_iid)
                except Exception as exc:
                    log.warning("onebound item_get enrichment failed: %s", exc)
                else:
                    detail = _merge_detail_enrichment(detail, onebound_detail)
            return detail
        return self._call_with_fallback("item_get", num_iid)

    def seller_info(self, sid: str) -> dict[str, Any]:
        if not self._ob:
            return {}
        return self._ob.seller_info(sid)

    def close(self) -> None:
        if self._mcp:
            self._mcp.close()

    def _init_clients(self) -> None:
        if self.mode in ("onebound", "hybrid"):
            try:
                self._ob = OneboundClient(self.cfg)
            except Exception as exc:
                if self.mode == "onebound":
                    raise
                log.warning("onebound unavailable in hybrid mode: %s", exc)
        if self.mode in ("mcp", "hybrid"):
            try:
                self._mcp = AlphashopMCPClient(self.cfg)
            except Exception as exc:
                if self.mode == "mcp":
                    raise
                log.warning("alphashop MCP unavailable in hybrid mode: %s", exc)
        if not self._ob and not self._mcp:
            raise RuntimeError("no ecom data source configured")

    def _call_with_fallback(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._mcp:
            try:
                return getattr(self._mcp, method)(*args, **kwargs)
            except Exception as exc:
                if self.mode != "hybrid" or not self._ob:
                    raise
                log.warning("MCP %s failed; fallback to onebound: %s", method, exc)
        if not self._ob:
            return [] if method.startswith("search") else {}
        return getattr(self._ob, method)(*args, **kwargs)


def make_data_client(data_source: str | None = None) -> HybridDataClient:
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
        "num_iid": str(raw.get("offerId") or "").strip(),
        "title": str(raw.get("originTitle") or raw.get("aiTitle") or "").strip(),
        "pic_url": str(raw.get("originImageUrl") or raw.get("aiImageUrl") or "").strip(),
        "detail_url": str(raw.get("detailUrl") or "").strip(),
        "price": raw.get("price"),
        "sales": _to_int(raw.get("soldOut")),
        "shopName": _shop_name_from(raw),
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
        sku_img = ""
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            name = str(attr.get("attributeName") or "").strip()
            value = str(attr.get("value") or "").strip()
            if name and value:
                pairs.append(f"{name}:{value}")
            if not sku_img:
                sku_img = str(attr.get("skuImageUrl") or "").strip()
        sku_rows.append({
            "sku_id": str(sku.get("skuId") or ""),
            "properties_name": ";".join(pairs),
            "quantity": _to_int(sku.get("amountOnSale")),
            "price": _to_float(sku.get("price")),
            "sku_image_url": sku_img,
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
        "num_iid": str(raw.get("offerId") or "").strip(),
        "title": str(raw.get("originTitle") or raw.get("aiTitle") or "").strip(),
        "pic_url": str(imgs[0]).strip() if isinstance(imgs, list) and imgs else "",
        "item_imgs": [{"url": str(u)} for u in imgs if u] if isinstance(imgs, list) else [],
        "sales": _to_int(raw.get("soldOut")),
        "min_num": _to_int(raw.get("minOrderQuantity")) or 1,
        "unit": "",
        "num": sum(_to_int(s.get("quantity")) for s in sku_rows),
        "price": sku_rows[0].get("price") if sku_rows else None,
        "skus": {"sku": sku_rows},
        "props": props,
        "seller_info": {
            "sid": str(raw.get("sellerId") or raw.get("supplierId") or raw.get("memberId") or ""),
            "nick": _shop_name_from(raw),
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
    return not (has_sid and has_shop and has_score and has_year)


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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_started()
        assert self._loop is not None and self._session is not None

        async def _do() -> Any:
            return await self._session.call_tool(name, arguments)

        future = asyncio.run_coroutine_threadsafe(_do(), self._loop)
        return _extract_tool_payload(future.result(timeout=self._timeout + 10))

    def close(self) -> None:
        if not self._loop or not self._loop.is_running():
            return

        async def _shutdown() -> None:
            if self._stack:
                await self._stack.aclose()

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive() and self._ready.is_set():
            if self._start_err:
                raise RuntimeError(f"MCP session failed to start: {self._start_err}")
            return
        with self._lock:
            if self._thread and self._thread.is_alive() and self._ready.is_set():
                if self._start_err:
                    raise RuntimeError(f"MCP session failed to start: {self._start_err}")
                return
            self._ready.clear()
            self._start_err = None
            self._thread = threading.Thread(target=self._thread_main, name="ecom-alphashop-mcp", daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=self._timeout + 10):
                raise RuntimeError("MCP session start timeout")
            if self._start_err:
                raise RuntimeError(f"MCP session failed to start: {self._start_err}")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_setup())
            self._ready.set()
            self._loop.run_forever()
        except Exception as exc:
            self._start_err = exc
            self._ready.set()

    async def _async_setup(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            sse_client(self._url_builder(), timeout=self._timeout)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()


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
    encoded = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items()))
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{path}_{encoded}")[:200]


def _cacheable_payload(payload: Any) -> bool:
    if isinstance(payload, dict) and isinstance(payload.get("result"), list) and not payload["result"]:
        return False
    return True


def _http_get_json(url: str, timeout: int) -> dict[str, Any]:
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
