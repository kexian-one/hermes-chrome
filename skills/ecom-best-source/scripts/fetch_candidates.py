from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from data_sources import make_data_client, merge_candidates, release_data_client
from product_match import relevance
from runtime_support import parallel_map, remaining_timeout


def fetch_candidates(
    query: str,
    extra_queries: list[str],
    image_urls: list[str],
    text_pages: int,
    image_pages: int,
    page_size: int,
    detail_top_k: int,
    data_source: str | None = None,
    target: dict[str, Any] | None = None,
    text_page_start: int = 1,
    image_page_start: int = 1,
) -> dict[str, Any]:
    client = make_data_client(data_source)
    try:
        collected: list[dict[str, Any]] = []
        queries = [query, *extra_queries]
        seen_queries = set()
        text_start, image_start = max(1, text_page_start), max(1, image_page_start)
        jobs = [("text", q, page) for q in queries if q and not (q in seen_queries or seen_queries.add(q)) for page in range(text_start, min(11, text_start + max(0, text_pages)))]
        jobs.extend(("image", u, p) for u in dict.fromkeys(image_urls) for p in range(image_start, min(11, image_start + max(0, image_pages))))
        runtime = client.cfg.runtime
        results = parallel_map(jobs, lambda job: (client.search if job[0] == "text" else client.search_image)(job[1], page=job[2], page_size=page_size), concurrency=int(runtime.get("concurrency", 3)), timeout=float(runtime.get("search_timeout_seconds", 120)))
        errors, searches = [], []
        for job, result in zip(jobs, results):
            if result["ok"]:
                rows = result["value"]
                searches.append({"channel": job[0], "query": job[1], "page": job[2], "count": len(rows)})
                for rank, row in enumerate(rows, 1):
                    item = dict(row)
                    item["retrieval_hits"] = [*item.get("retrieval_hits", []), {"channel": job[0], "query": job[1], "page": job[2], "rank": rank, "provider": item.get("provider") or data_source or "configured"}]
                    collected.append(item)
            else:
                errors.append({"channel": job[0], "query": job[1], "page": job[2], "error": result["error"]})
        if jobs and all(not result["ok"] for result in results):
            raise RuntimeError("所有搜索通道失败，不能判定为无供给")
        candidates = sorted(merge_candidates(collected), key=lambda item: relevance(item, target or {"title": query}), reverse=True)
        def enrich(num_iid: str) -> dict:
            if not num_iid:
                return {}
            detail = copy.deepcopy(client.item_get(num_iid))
            update = {}
            if detail:
                update["detail"] = detail
                seller = detail.get("seller_info") if isinstance(detail, dict) else {}
                sid = (seller or {}).get("sid") if isinstance(seller, dict) else ""
                if sid:
                    try:
                        update["seller_info"] = copy.deepcopy(client.seller_info(str(sid)))
                    except Exception as exc:
                        update.setdefault("fetch_errors", []).append(type(exc).__name__)
            return update
        detail_results = parallel_map([str(candidate.get("num_iid") or "") for candidate in candidates[:max(0, detail_top_k)]], enrich, concurrency=int(runtime.get("concurrency", 3)), timeout=float(runtime.get("detail_timeout_seconds", 120)))
        for candidate, result in zip(candidates, detail_results):
            remaining_timeout(1)
            if result["ok"]:
                candidate.update(result["value"])
        errors.extend({"channel": "detail", "offer_id": c.get("num_iid"), "error": r["error"]} for c, r in zip(candidates, detail_results) if not r["ok"])
        return {
            "query": query,
            "extra_queries": extra_queries,
            "image_urls": image_urls,
            "candidates": candidates,
            "stats": client.stats,
            "searches": searches,
            "errors": errors,
            "partial": bool(errors),
        }
    finally:
        release_data_client(client)


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
