from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from urllib.parse import urlsplit
from html.parser import HTMLParser
from typing import Any
from identifier_sources import identifiers_from_data


ITEM_ID_RE = re.compile(r"(?:item\.jd\.com/|item\.m\.jd\.com/product/|(?:sku|skuId|wareId)=|goods-detail/)(\d+)", re.IGNORECASE)
SCRIPT_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class ProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[dict[str, str]] = []
        self.title_parts: list[str] = []
        self.canonical_url = ""
        self.product_images: list[str] = []
        self._elements: list[tuple[str, dict[str, str]]] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
            self.meta.append(attr)
        elif tag.lower() == "link" and "canonical" in attr.get("rel", "").lower().split():
            self.canonical_url = attr.get("href", "")
        elif tag.lower() == "title":
            self._in_title = True
        if tag == "img":
            markers = [" ".join(a.get(k, "") for k in ("id", "class")).lower() for _, a in self._elements]
            gallery = any(any(key in marker for key in ("spec-n1", "spec-list", "gallery", "preview")) for marker in markers)
            gallery |= any("goodsdetail" in marker for marker in markers) and any("image" in marker for marker in markers)
            recommendation = any(any(key in marker for key in ("recommend", "suggest", "猜你喜欢")) for marker in markers)
            if gallery and not recommendation:
                self.product_images.extend(attr[key] for key in ("data-origin", "data-original", "data-src", "data-lazy-img", "src") if attr.get(key))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._elements.append((tag, attr))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag.lower():
                del self._elements[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def fetch_product(url: str, timeout: int = 30) -> dict[str, Any]:
    text = _http_get_text(url, timeout)
    parsed = parse_product_html(text, url)
    item_id = str(parsed.get("item_id") or _item_id(url) or "")
    if item_id and _is_generic_result(parsed):
        parsed = _best_product_result([
            parsed,
            *_fetch_fallback_products(item_id, timeout),
        ])
    if not parsed.get("title") and not parsed.get("main_image_url"):
        raise RuntimeError("could not extract JD title or image from page")
    return parsed


def parse_product_html(text: str, url: str) -> dict[str, Any]:
    parser = ProductHTMLParser()
    parser.feed(text)

    meta = parser.meta
    title = (
        _first_meta(meta, "property", "og:title")
        or _first_meta(meta, "name", "title")
        or _first_meta(meta, "name", "keywords")
        or " ".join(parser.title_parts)
    )
    title = _clean_title(title)

    requested_id = _item_id(url)
    page_ids = {_item_id(value) for value in (
        parser.canonical_url, _first_meta(meta, "property", "og:url"),
    ) if _item_id(value)}
    conflicting_page = bool(requested_id and any(value != requested_id for value in page_ids))
    item_id = requested_id or (next(iter(page_ids)) if len(page_ids) == 1 else "")
    structured = _jsonld_product(text, item_id)
    if structured and _title_score(title) <= 0:
        title = _clean_title(str(structured.get("name") or ""))
    images = [u for u in _image_values(structured.get("image")) if _supported_product_image(u)] if structured else []
    scope = "selected_sku" if images else "product_page"
    image_source = "jsonld_sku" if images else "page_metadata"
    if not images and item_id and _title_score(title) > 0:
        images = [_first_meta(meta, "property", "og:image"), _first_meta(meta, "name", "image"), *parser.product_images]
        image_source = "page_metadata_or_gallery"
    image_urls = _unique(_normalize_image_url(x) for x in images if _supported_product_image(x)) if not conflicting_page else []

    return {
        "title": title,
        "jd_url": url,
        "item_id": item_id,
        "main_image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls[:12],
        "image_urls_scope": scope if image_urls else "unconfirmed",
        "image_evidence": [{"url": image, "source": image_source, "item_id": item_id} for image in image_urls[:12]],
        "target_errors": ["京东页面商品 ID 与请求 SKU 不一致"] if conflicting_page else [],
        "identifiers": identifiers_from_data(structured, "jd_jsonld_sku") if structured and not conflicting_page else [],
    }


def _fetch_fallback_products(item_id: str, timeout: int) -> list[dict[str, Any]]:
    products = []
    for url in (
        f"https://item.jd.com/{item_id}.html",
        f"https://item.m.jd.com/product/{item_id}.html",
    ):
        try:
            products.append(parse_product_html(_http_get_text(url, timeout), url))
        except Exception:
            continue
    return products


def _best_product_result(products: list[dict[str, Any]]) -> dict[str, Any]:
    if not products:
        return {}
    item_id = str(products[0].get("item_id") or "")
    eligible = [p for p in products if not p.get("target_errors") and (not item_id or str(p.get("item_id") or "") == item_id)]
    if not eligible:
        return dict(products[0])
    return dict(max(eligible, key=lambda p: (_title_score(str(p.get("title") or "")), bool(p.get("main_image_url")))))


def _http_get_text(url: str, timeout: int) -> str:
    try:
        from curl_cffi import requests as crequests

        response = crequests.get(url, timeout=timeout, impersonate="chrome120")
        response.raise_for_status()
        return response.text
    except Exception:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")


def _first_meta(meta: list[dict[str, str]], key: str, value: str) -> str:
    for item in meta:
        if item.get(key, "").lower() == value.lower():
            return html.unescape(item.get("content", "")).strip()
    return ""


def _jsonld_product(text: str, item_id: str) -> dict[str, Any]:
    if not item_id:
        return {}
    for match in SCRIPT_JSON_RE.finditer(text):
        raw = html.unescape(match.group(1)).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(graph)
            kinds = node.get("@type") or []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if "Product" not in kinds:
                continue
            ids = {str(node[key]) for key in ("sku", "skuId", "productID") if node.get(key) is not None}
            ids.update(_item_id(str(node.get(key) or "")) for key in ("url", "@id"))
            ids.discard("")
            if ids == {item_id}:
                return node
    return {}


def _image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return _image_values(value.get("contentUrl") or value.get("url"))
    if isinstance(value, list):
        return [image for entry in value for image in _image_values(entry)]
    return []


def _clean_title(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[-_]\s*(京东|JD\.COM|京东商城).*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"[,，]\s*[^,，]{1,40}[,，]*\s*京东.*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"[,，]\s*京东.*$", "", text, flags=re.IGNORECASE).strip()
    return text


def _normalize_image_url(value: str) -> str:
    url = html.unescape(value).strip().rstrip(",;")
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r"!(?:q\d+|cc_\d+x\d+|.*?\.webp).*$", "", url)
    url = re.sub(r"\.webp$", "", url)
    url = url.rstrip(")")
    return url


def _supported_product_image(value: str) -> bool:
    try:
        parsed = urlsplit(_normalize_image_url(value))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return bool(parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
                and any(host == suffix or host.endswith("." + suffix) for suffix in ("360buyimg.com", "jdimg.com"))
                and not re.search(r"imagetools|placeholder|/logo|/blank|\.gif$", parsed.path, re.I))


def _item_id(value: str) -> str:
    match = ITEM_ID_RE.search(value or "")
    return match.group(1) if match else ""


def _unique(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _is_generic_result(product: dict[str, Any]) -> bool:
    return _title_score(str(product.get("title") or "")) <= 0


def _title_score(title: str) -> int:
    text = (title or "").strip()
    if not text or text in {"京东", "京东万商", "京东商城", "JD.COM"}:
        return 0
    score = min(len(text), 80)
    if any(word in text for word in ("网上购物", "京东", "登录")):
        score -= 20
    if "..." in text or "…" in text:
        score -= 30
    if re.search(r"[\u4e00-\u9fff].*(g|ml|kg|L|克|毫升|瓶|箱)", text, re.IGNORECASE):
        score += 20
    return score


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract JD/B2B product title and images from URL")
    parser.add_argument("--url", required=True, help="item.jd.com or b2b.jd.com product URL")
    parser.add_argument("--output", help="Output JSON path")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    result = fetch_product(args.url, timeout=args.timeout)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
