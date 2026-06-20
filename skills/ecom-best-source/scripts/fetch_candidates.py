from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from data_sources import make_data_client, merge_candidates


def fetch_candidates(
    query: str,
    extra_queries: list[str],
    image_urls: list[str],
    text_pages: int,
    image_pages: int,
    page_size: int,
    detail_top_k: int,
    data_source: str | None = None,
) -> dict[str, Any]:
    client = make_data_client(data_source)
    try:
        collected: list[dict[str, Any]] = []
        queries = [query, *extra_queries]
        seen_queries = set()
        for q in [q for q in queries if q and not (q in seen_queries or seen_queries.add(q))]:
            for page in range(1, max(1, text_pages) + 1):
                collected.extend(client.search(q, page=page, page_size=page_size))
        for image_url in image_urls:
            for page in range(1, max(1, image_pages) + 1):
                collected.extend(client.search_image(image_url, page=page, page_size=50))

        candidates = merge_candidates(collected)
        for candidate in candidates[:max(0, detail_top_k)]:
            num_iid = str(candidate.get("num_iid") or "")
            if not num_iid:
                continue
            detail = client.item_get(num_iid)
            if detail:
                candidate["detail"] = detail
                seller = detail.get("seller_info") if isinstance(detail, dict) else {}
                sid = (seller or {}).get("sid") if isinstance(seller, dict) else ""
                if sid:
                    candidate["seller_info"] = client.seller_info(str(sid))
        return {
            "query": query,
            "extra_queries": extra_queries,
            "image_urls": image_urls,
            "candidates": candidates,
            "stats": client.stats,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch normalized 1688 candidates for ecom-best-source")
    parser.add_argument("--query", required=True, help="1688 search query")
    parser.add_argument("--extra-query", action="append", default=[], help="Additional text query; repeatable")
    parser.add_argument("--image-url", action="append", default=[], help="JD image URL; repeatable")
    parser.add_argument("--text-pages", type=int, default=2)
    parser.add_argument("--image-pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--detail-top-k", type=int, default=6)
    parser.add_argument("--data-source", choices=["onebound", "mcp", "hybrid"], help="Override config data_source")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    result = fetch_candidates(
        query=args.query,
        extra_queries=args.extra_query,
        image_urls=args.image_url,
        text_pages=args.text_pages,
        image_pages=args.image_pages,
        page_size=args.page_size,
        detail_top_k=args.detail_top_k,
        data_source=args.data_source,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
