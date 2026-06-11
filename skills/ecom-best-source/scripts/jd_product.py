from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from typing import Any


JD_IMAGE_RE = re.compile(
    r"(?:https?:)?//img\d{2,}\.360buyimg\.com/[^\s\"'<>\\]+",
    re.IGNORECASE,
)
ITEM_ID_RE = re.compile(r"(?:item\.jd\.com/|sku=|wareId=|goods-detail/)(\d+)", re.IGNORECASE)
SCRIPT_JSON_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class ProductHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[dict[str, str]] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "meta":
            self.meta.append(attr)
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def fetch_product(url: str, timeout: int = 30) -> dict[str, Any]:
    text = _http_get_text(url, timeout)
    parsed = parse_product_html(text, url)
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

    images: list[str] = []
    for value in (
        _first_meta(meta, "property", "og:image"),
        _first_meta(meta, "name", "image"),
        _jsonld_image(text),
    ):
        if value:
            images.append(value)
    images.extend(JD_IMAGE_RE.findall(text))
    image_urls = _unique(_normalize_image_url(x) for x in images if x)

    return {
        "title": title,
        "jd_url": url,
        "item_id": _item_id(url) or _item_id(text),
        "main_image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls[:12],
    }


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


def _jsonld_image(text: str) -> str:
    for match in SCRIPT_JSON_RE.finditer(text):
        raw = html.unescape(match.group(1)).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        image = data.get("image") if isinstance(data, dict) else None
        if isinstance(image, str):
            return image
        if isinstance(image, list) and image:
            return str(image[0])
    return ""


def _clean_title(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[-_]\s*(京东|JD\.COM|京东商城).*$", "", text, flags=re.IGNORECASE).strip()
    return text


def _normalize_image_url(value: str) -> str:
    url = html.unescape(value).strip().rstrip(",;")
    if url.startswith("//"):
        url = "https:" + url
    url = re.sub(r"!(?:q\d+|cc_\d+x\d+|.*?\.webp).*$", "", url)
    url = re.sub(r"\.webp$", "", url)
    return url


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
