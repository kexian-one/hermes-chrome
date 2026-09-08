from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path

from agent.builtin_tools import _extract_jd_product_browser
from agent.config import WorkerConfig
from agent.ecom_modules import load_scripts
from agent.llm_client import LLMClient
from agent.mcp_client import OpenClaudeInChromeClient
from agent.product_vision import ProductVision

load_scripts()
from ecom_config import load_ecom_config
from data_sources import merge_candidates, task_data_session
from fetch_candidates import fetch_candidates
from jd_product import fetch_product
from keyword_builder import build_keywords
from product_match import ATTRIBUTE_KEYS, target_attributes, required_attribute_keys
from runtime_support import atomic_json, cache_key, cancellation_scope, deadline, remaining_timeout
from sourcing_pipeline import _run_with_final_detail_confirmation, run_from_files, select_confirmation_batch, supplier_key
from sourcing_rules import run_pipeline

log = logging.getLogger(__name__)
JD_URL = re.compile(r"https?://(?:item\.jd\.com/\d+\.html|b2b\.jd\.com/[^\s<>\"，。]+)")


async def _run_sync_stage(operation, *args, expires: float | None = None, **kwargs):
    token = deadline.set(min(deadline.get(), expires if expires is not None else float("inf")))
    try:
        remaining_timeout(1)
        return await asyncio.to_thread(operation, *args, **kwargs)
    finally:
        deadline.reset(token)


def select_refresh_candidates(payload: dict, backup_suppliers: int = 2) -> list[dict]:
    output = {**(payload.get("config") or {}).get("output", {}), "target_count": 6 + max(0, min(2, backup_suppliers))}
    result = run_pipeline({**payload, "config": {**(payload.get("config") or {}), "output": output}})
    candidates_by_id = {str(candidate.get("num_iid") or candidate.get("offerId") or ""): candidate for candidate in payload.get("candidates", [])}
    selected = [candidates_by_id[str(item.get("num_iid") or item.get("offerId") or "")] for item in result["final"]]
    return [candidate for candidate in selected if candidate.get("detail") and time.time() - float(candidate["detail"].get("fetched_at") or 0) > 60]


def merge_product(browser: dict, static: dict) -> dict:
    merged = dict(browser)
    if static.get("target_errors"):
        return merged or {"target_errors": static["target_errors"]}
    if browser.get("item_id") and static.get("item_id") and str(browser["item_id"]) != str(static["item_id"]):
        merged.setdefault("target_errors", []).append("浏览器与静态页面 SKU ID 不一致")
        return merged
    if not browser:
        return dict(static)
    for key in ("title", "item_id", "jd_url"):
        if not merged.get(key) and static.get(key):
            merged[key] = static[key]
    if not merged.get("main_image_url") and static.get("main_image_url"):
        for key in ("main_image_url", "image_urls", "image_urls_scope", "image_evidence"):
            merged.pop(key, None)
            if key in static:
                merged[key] = static[key]
    return merged


def _expansion_plan(product: dict, fetched: dict, mode: str, max_pages: int) -> list[dict]:
    attrs = target_attributes(product)
    primary = str(fetched.get("query") or " ".join(str(value) for value in attrs.values()))
    images = list(fetched.get("image_urls") or [])[:1]
    initial_queries = {primary, *(fetched.get("extra_queries") or [])}
    plans = []
    if mode == "hybrid":
        plans.append({"query": primary, "images": images, "provider": "onebound", "page": 1, "kind": "secondary_source"})
    rewrites = [" ".join(str(attrs[key]) for key in keys if attrs.get(key)) for keys in (("brand", "category", "series", "flavor"), ("brand", "category", "model"), ("brand", "category"))]
    seen = set(initial_queries)
    for query in rewrites:
        if query and query not in seen:
            plans.append({"query": query, "images": [], "provider": mode, "page": 1, "kind": "query_rewrite"})
            seen.add(query)
    initial_pages = max((int(search.get("page", 1)) for search in fetched.get("searches", []) if search.get("channel") == "text"), default=1)
    for page in range(initial_pages + 1, max_pages + 1):
        plans.append({"query": primary, "images": images, "provider": mode, "page": page, "kind": "next_page"})
        if mode == "hybrid":
            plans.append({"query": primary, "images": [], "provider": "onebound", "page": page, "kind": "secondary_page"})
    return plans


async def retrieve_qualified_candidates(product: dict, fetched: dict, cfg, vision: ProductVision, *, checkpoint=None, deadline_at: float | None = None) -> tuple[dict, dict]:
    runtime = cfg.runtime
    target_count = 6
    batch_size = max(8, min(12, int(runtime.get("retrieval_batch_size", 10))))
    detail_limit = max(1, int(runtime.get("retrieval_max_details", 80)))
    round_limit = max(1, int(runtime.get("retrieval_max_rounds", 12)))
    candidate_limit = max(batch_size, int(runtime.get("retrieval_max_candidates", 500)))
    search_limit = max(0, int(runtime.get("retrieval_max_search_rounds", 8)))
    vision_limit = max(0, int(runtime.get("vision_max_pairs", cfg.vision.get("max_pairs", 60))))
    expires = min(deadline_at or float("inf"), time.monotonic() + float(runtime.get("retrieval_timeout_seconds", 480)))
    plans = _expansion_plan(product, fetched, cfg.data_source, max(1, min(10, int(runtime.get("retrieval_max_pages", 4)))))
    fetched["candidates"] = merge_candidates(fetched.get("candidates") or [])
    payload = {"target": product, "candidates": fetched["candidates"], "config": {"output": {"target_count": target_count}}}
    report = {"rounds": [], "search_rounds": [], "errors": [], "detail_attempts": 0, "vision_attempts": 0, "qualified_suppliers": 0, "stop_reason": "", "budget": {"details": detail_limit, "vision_pairs": vision_limit, "rounds": round_limit, "search_rounds": search_limit, "candidates": candidate_limit}, "stage_seconds": {"search": 0.0, "details": 0.0, "vision": 0.0}}
    previous_report = fetched.get("retrieval") or {}
    for key in ("rounds", "search_rounds", "errors"):
        report[key] = list(previous_report.get(key) or [])
    for key in ("detail_attempts", "vision_attempts"):
        report[key] = max(0, int(previous_report.get(key) or 0))
    completed_searches = {(search.get("provider"), search.get("query"), search.get("page")) for search in report["search_rounds"]}
    plans = [plan for plan in plans if (plan["provider"], plan["query"], plan["page"]) not in completed_searches]
    previous_confirmation = previous_report.get("confirmation") or {}
    confirmations = {"attempted": int(previous_confirmation.get("attempted") or 0), "confirmed": int(previous_confirmation.get("confirmed") or 0), "errors": list(previous_confirmation.get("errors") or [])}
    attempted = {str(c.get("num_iid") or c.get("offerId") or c.get("title")) for c in payload["candidates"] if c.get("_retrieval_checked")}
    supplier_attempts: dict[str, int] = {}
    vision_pending = [candidate for candidate in payload["candidates"] if product.get("require_vision") and candidate.get("_retrieval_checked") and candidate.get("detail")]
    exhausted: set[tuple[str, str]] = set()
    def persist() -> None:
        fetched["retrieval"] = report
        if checkpoint:
            checkpoint(fetched, report)
    while True:
        qualified = run_pipeline(payload)["final"]
        report["qualified_suppliers"] = len(qualified)
        if len(qualified) >= target_count:
            report["stop_reason"] = "enough_qualified_suppliers"
            break
        if product.get("require_vision") and report["vision_attempts"] >= vision_limit:
            report["stop_reason"] = "vision_budget_exhausted"
            break
        if time.monotonic() >= expires:
            report["stop_reason"] = "time_budget_exhausted"
            break
        if len(report["rounds"]) >= round_limit:
            report["stop_reason"] = "round_budget_exhausted"
            break
        remaining = [c for c in payload["candidates"] if str(c.get("num_iid") or c.get("offerId") or c.get("title")) not in attempted]
        can_expand = bool(plans) and len(report["search_rounds"]) < search_limit and len(payload["candidates"]) < candidate_limit
        suppliers_left = {supplier_key(c) for c in remaining}
        need_expansion = not remaining or (bool(report["rounds"]) and len(suppliers_left) + len(qualified) < target_count)
        if can_expand and need_expansion and not vision_pending and report["detail_attempts"] < detail_limit:
            plan = plans.pop(0)
            if (plan["provider"], plan["query"]) in exhausted:
                continue
            before = len(payload["candidates"])
            stage_start = time.monotonic()
            search_report = dict(plan)
            try:
                extra = await _run_sync_stage(fetch_candidates, plan["query"], [], plan["images"], 1, int(bool(plan["images"])), 50, 0, plan["provider"], product, plan["page"], plan["page"], expires=expires)
                merged = merge_candidates([*payload["candidates"], *(extra.get("candidates") or [])])
                payload["candidates"] = fetched["candidates"] = merged[:candidate_limit]
                search_report.update({"new_candidates": len(payload["candidates"]) - before, "stats": extra.get("stats"), "searches": extra.get("searches", []), "errors": extra.get("errors", [])})
                report["errors"].extend(extra.get("errors") or [])
                if not extra.get("candidates") and not extra.get("errors"):
                    exhausted.add((plan["provider"], plan["query"]))
            except Exception as exc:
                search_report["error"] = type(exc).__name__
                report["errors"].append({"stage": "search", "provider": plan["provider"], "error": type(exc).__name__})
            report["search_rounds"].append(search_report)
            report["stage_seconds"]["search"] += time.monotonic() - stage_start
            persist()
            continue
        can_detail = report["detail_attempts"] < detail_limit
        batch = select_confirmation_batch(remaining, product, min(batch_size, detail_limit - report["detail_attempts"]), 2, supplier_attempts) if remaining and can_detail else []
        if not batch and not vision_pending:
            if remaining and not can_detail:
                report["stop_reason"] = "detail_budget_exhausted"
            elif len(report["search_rounds"]) >= search_limit and plans:
                report["stop_reason"] = "search_budget_exhausted"
            elif len(payload["candidates"]) >= candidate_limit and plans:
                report["stop_reason"] = "candidate_budget_exhausted"
            else:
                report["stop_reason"] = "searched_scope_exhausted"
            break
        round_report = {"index": len(report["rounds"]) + 1, "offer_ids": [], "detail_attempts": len(batch), "vision_attempts": 0}
        if batch:
            stage_start = time.monotonic()
            try:
                result = await _run_sync_stage(_run_with_final_detail_confirmation, {**payload, "candidates": batch}, target_count, len(batch), expires=expires)
            except TimeoutError:
                report["detail_attempts"] += len(batch)
                report["stage_seconds"]["details"] += time.monotonic() - stage_start
                report["errors"].append({"stage": "details", "error": "TimeoutError"})
                report["stop_reason"] = "time_budget_exhausted"
                report["rounds"].append(round_report)
                break
            confirmation = result.get("confirmation") or {}
            for key in ("attempted", "confirmed"):
                confirmations[key] += int(confirmation.get(key) or 0)
            confirmations["errors"].extend(confirmation.get("errors") or [])
            if confirmation.get("error"):
                confirmations["errors"].append({"error": confirmation["error"]})
            report["detail_attempts"] += len(batch)
            report["stage_seconds"]["details"] += time.monotonic() - stage_start
            for candidate in batch:
                offer_id = str(candidate.get("num_iid") or candidate.get("offerId") or candidate.get("title"))
                attempted.add(offer_id)
                candidate["_retrieval_checked"] = True
                key = supplier_key(candidate)
                supplier_attempts[key] = supplier_attempts.get(key, 0) + 1
                round_report["offer_ids"].append(offer_id)
        verify_batch = [*vision_pending, *batch]
        vision_pending = []
        if product.get("require_vision") and verify_batch:
            pairs_left = vision_limit - report["vision_attempts"]
            if pairs_left <= 0:
                report["stop_reason"] = "vision_budget_exhausted"
                report["rounds"].append(round_report)
                break
            stage_start = time.monotonic()
            previous = dict(vision.metrics)
            verification = await vision.verify_candidates(product, verify_batch, min(24, pairs_left))
            used = int(verification.get("attempted", 0)) if isinstance(verification, dict) else max(0, int(vision.metrics.get("verified_skus", 0)) - int(previous.get("verified_skus", 0)))
            if not isinstance(verification, dict) and not used:
                used = sum(max(0, int(vision.metrics.get(key, 0)) - int(previous.get(key, 0))) for key in ("calls", "cache_hits", "errors"))
            report["vision_attempts"] += used
            round_report["vision_attempts"] = used
            if isinstance(verification, dict) and verification.get("remaining"):
                vision_pending = verify_batch
                if not used:
                    report["stop_reason"] = "vision_no_progress"
            report["stage_seconds"]["vision"] += time.monotonic() - stage_start
        round_report["qualified_suppliers"] = len(run_pipeline(payload)["final"])
        report["rounds"].append(round_report)
        persist()
        if report["stop_reason"]:
            break
    report["qualified_suppliers"] = len(run_pipeline(payload)["final"])
    report["candidate_count"] = len(payload["candidates"])
    report["unexamined_count"] = sum(not c.get("_retrieval_checked") for c in payload["candidates"])
    report["confirmation"] = confirmations
    report["errors"].extend(confirmations["errors"])
    payload["retrieval"] = report
    payload["partial"] = bool(report["errors"] or report["stop_reason"] not in {"enough_qualified_suppliers", "searched_scope_exhausted"})
    persist()
    return payload, report


async def run_sourcing(config: WorkerConfig, task: str, llm: LLMClient, vision_llm: LLMClient) -> int:
    urls = JD_URL.findall(task)
    if len(set(urls)) != 1:
        log.error("找货任务需要一个明确的京东商品链接")
        return 1
    root = Path(os.environ.get("WORKER_PROJECT_ROOT") or Path.cwd()).resolve()
    output = Path(os.environ.get("WORKER_OUTPUT_DIR") or root / "outputs" / ("sourcing-" + time.strftime("%Y%m%d-%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch = output / ".ecom-scratch"
    scratch.mkdir(exist_ok=True)
    cfg = load_ecom_config(root)
    vision = ProductVision(vision_llm, root / "outputs" / ".ecom-best-source-cache" / "vision", int(cfg.vision.get("concurrency", 3)), float(cfg.vision.get("timeout_seconds", 45)), image_cache_seconds=float(cfg.vision.get("image_cache_seconds", 3600)))
    run_id = cache_key("task", [task, str(output)])
    metrics: dict = {"run_id": run_id, "model": config.llm_multimodal.model, "stages": {}, "errors": []}
    started = time.monotonic()
    checkpoint = scratch / "checkpoint.json"
    resume: Path | None = None
    resume_raw = os.environ.get("WORKER_RESUME_DIR")
    if resume_raw:
        proposed = Path(resume_raw).resolve()
        if proposed.is_relative_to(root / "outputs"):
            try:
                previous = json.loads((proposed / ".ecom-scratch" / "checkpoint.json").read_text(encoding="utf-8"))
                if previous.get("task") == task and previous.get("stage") != "completed":
                    resume = proposed / ".ecom-scratch"
            except (OSError, ValueError, TypeError):
                pass
    def recover(name: str, ttl: float) -> dict | None:
        if resume is None:
            return None
        try:
            path = resume / name
            if time.time() - path.stat().st_mtime < ttl:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    metrics.setdefault("resumed", []).append(name)
                    return value
        except (OSError, ValueError, TypeError):
            pass
        return None
    def save(stage: str, **data: object) -> None:
        atomic_json(checkpoint, {"run_id": run_id, "task": task, "stage": stage, "updated_at": time.time(), **data})
        log.info("ecom stage=%s elapsed=%.2f", stage, time.monotonic() - started)
    async def execute() -> int:
        stage_start = time.monotonic()
        save("target")
        product: dict = recover("jd_product.json", 60) or {}
        async with AsyncExitStack() as stack:
            try:
                if not product:
                    async with asyncio.timeout(15):
                        mcp = await stack.enter_async_context(OpenClaudeInChromeClient(port=config.mcp_port, mcp_server_js_path=config.mcp_server_js_path, require_bridge=True))
                    response = json.loads(await asyncio.wait_for(_extract_jd_product_browser({"url": urls[0], "output": str(scratch / "jd_product.json"), "wait_seconds": 15}, root, mcp=mcp), 45))
                    if response.get("ok"):
                        product = json.loads((scratch / "jd_product.json").read_text(encoding="utf-8"))
            except Exception as exc:
                metrics["errors"].append({"stage": "browser", "error": type(exc).__name__})
        if not all(product.get(k) for k in ("title", "item_id", "main_image_url")):
            try:
                static = await asyncio.to_thread(fetch_product, urls[0], 15)
                product = merge_product(product, static)
            except Exception as exc:
                metrics["errors"].append({"stage": "static", "error": type(exc).__name__})
        if not product.get("title") or not product.get("item_id"):
            raise ValueError("无法取得目标商品资料")
        task_response = await llm.structured("从用户原文提取明确要求。返回 {quantity: 正整数或null, user_attributes: 属性对象, destination: {province,city,district}或null, allow_pack_substitution: false}。允许的商品属性字段为 " + ", ".join(sorted(ATTRIBUTE_KEYS)) + "。quantity 是要采购多少销售单位，不是规格中的袋数；destination 只保留用户明确提供的收货省市区，不根据商品产地、链接或常识推断，没提供就返回null。不得补出用户没有要求的属性。仅明确允许不同包装时 allow_pack_substitution 为 true。", {"user_message": task})
        intent = json.loads(task_response.text or "{}")
        metrics["intent"] = {"elapsed_seconds": task_response.elapsed_seconds, "usage": task_response.usage}
        if task_response.finish_reason != "stop" or not isinstance(intent, dict) or not isinstance(intent.get("user_attributes") or {}, dict):
            raise ValueError("用户要求解析不完整")
        product["user_attributes"] = {k: v for k, v in (intent.get("user_attributes") or {}).items() if k in ATTRIBUTE_KEYS and v not in (None, "")}
        destination = intent.get("destination")
        if destination is not None and (not isinstance(destination, dict) or any(not isinstance(value, str) for value in destination.values() if value not in (None, ""))):
            raise ValueError("收货地区解析不完整")
        product["destination"] = {key: value.strip() for key, value in (destination or {}).items() if key in {"province", "city", "district"} and isinstance(value, str) and value.strip()}
        quantity = intent.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0):
            raise ValueError("采购数量必须是正整数")
        product["buy_multiple"] = quantity or product.get("buy_multiple") or 1
        product["allow_pack_substitution"] = intent.get("allow_pack_substitution") is True
        kw = build_keywords(str(product["title"]), product.get("brand") or product["user_attributes"].get("brand"))
        product = {**kw.to_target(product["title"]), **product}
        product["attributes"] = target_attributes(product)
        if cfg.vision.get("enabled", True):
            try:
                product = await vision.extract_target(product)
            except Exception as exc:
                metrics["errors"].append({"stage": "target_vision", "error": type(exc).__name__})
        product["attributes"].update(product["user_attributes"])
        product["require_vision"] = bool(cfg.vision.get("enabled", True))
        required = required_attribute_keys(product["attributes"])
        product["unconfirmed_attributes"] = sorted(required - {key for key, value in product["attributes"].items() if value not in (None, "")})
        if product.get("target_errors"):
            raise ValueError("目标商品规格证据冲突，请核对已选 SKU")
        if not product["attributes"].get("brand") or not product["attributes"].get("category"):
            raise ValueError("无法确认目标品牌或品类，不能自动推荐货源")
        product["brand"] = product["attributes"]["brand"]
        product["category"] = product["attributes"]["category"]
        metrics["target"] = {"category": product["category"], "item_id": product["item_id"]}
        atomic_json(scratch / "jd_product.json", product)
        metrics["stages"]["target"] = time.monotonic() - stage_start
        save("search", target=product)
        stage_start = time.monotonic()
        attrs = product["attributes"]
        primary = " ".join(str(attrs[k]) for k in ("brand", "category", "series", "flavor", "color", "form", "sugar_content", "fat_content", "net_content", "pack_count", "packaging", "packaging_variant", "model") if attrs.get(k))
        queries = [" ".join(str(attrs[k]) for k in ("brand", "category", "flavor", "net_content") if attrs.get(k)), *build_keywords(primary, product["brand"]).extra_queries()]
        images = [product["main_image_url"]] if product.get("main_image_url") else []
        fetched = recover("candidates.json", 300) or await asyncio.to_thread(fetch_candidates, primary, queries[:3], images, 1, 1, 50, 0, cfg.data_source, product)
        fetched.setdefault("query", primary)
        fetched.setdefault("extra_queries", queries[:3])
        fetched.setdefault("image_urls", images)
        atomic_json(scratch / "candidates.json", fetched)
        metrics["search"] = fetched.get("stats")
        metrics["errors"].extend(fetched.get("errors") or [])
        metrics["stages"]["search"] = time.monotonic() - stage_start
        save("details", target=product)
        def retrieval_checkpoint(current: dict, report: dict) -> None:
            atomic_json(scratch / "candidates.json", current)
            save("retrieval", target=product, retrieval=report)
        task_budget = float(cfg.runtime.get("task_timeout_seconds", 600))
        payload, retrieval = await retrieve_qualified_candidates(product, fetched, cfg, vision, checkpoint=retrieval_checkpoint, deadline_at=started + task_budget - min(30, task_budget * 0.1))
        metrics["retrieval"] = retrieval
        metrics["confirmation"] = retrieval["confirmation"]
        metrics["errors"].extend(retrieval["errors"])
        for stage, duration in retrieval["stage_seconds"].items():
            metrics["stages"][stage] = metrics["stages"].get(stage, 0.0) + duration
        metrics["vision"] = vision.metrics
        save("refresh", target=product)
        stage_start = time.monotonic()
        stale = select_refresh_candidates(payload, int(cfg.runtime.get("refresh_backup_suppliers", 2)))
        refresh_budget = max(0, retrieval["budget"]["details"] - retrieval["detail_attempts"])
        if stale and refresh_budget:
            refreshed = await asyncio.to_thread(_run_with_final_detail_confirmation, {**payload, "candidates": stale[:refresh_budget]}, 6, min(len(stale), refresh_budget), force_refresh=True)
            metrics["refresh"] = refreshed.get("confirmation") or {}
            retrieval["detail_attempts"] += int(metrics["refresh"].get("attempted") or 0)
        for candidate in payload["candidates"]:
            if candidate.get("detail") and time.time() - float(candidate["detail"].get("fetched_at") or 0) > 60:
                candidate["detail_error"] = "价格库存数据已过期"
        metrics["stages"]["refresh"] = time.monotonic() - stage_start
        if stale and len(run_pipeline(payload)["final"]) < 6:
            payload, retrieval = await retrieve_qualified_candidates(product, fetched, cfg, vision, checkpoint=retrieval_checkpoint, deadline_at=started + task_budget - min(30, task_budget * 0.1))
            metrics["retrieval"] = retrieval
            metrics["confirmation"] = retrieval["confirmation"]
            for stage, duration in retrieval["stage_seconds"].items():
                metrics["stages"][stage] = metrics["stages"].get(stage, 0.0) + duration
        for candidate in payload["candidates"]:
            if candidate.get("detail") and time.time() - float(candidate["detail"].get("fetched_at") or 0) > 60:
                candidate["detail_error"] = "价格库存数据已过期"
        retrieval["qualified_suppliers"] = len(run_pipeline(payload)["final"])
        if retrieval["stop_reason"] == "enough_qualified_suppliers" and retrieval["qualified_suppliers"] < 6:
            retrieval["stop_reason"] = "freshness_validation_incomplete"
            payload["partial"] = True
        save("export", target=product)
        confirmations = [metrics.get("confirmation") or {}, metrics.get("refresh") or {}]
        payload["partial"] = bool(payload.get("partial") or fetched.get("errors") or any(c.get("errors") or c.get("error") for c in confirmations) or vision.metrics["errors"])
        atomic_json(scratch / "input.json", payload)
        summary = await asyncio.to_thread(run_from_files, jd_product_path=None, candidates_path=None, merged_input_path=str(scratch / "input.json"), output_path=str(output / "找货结果.csv"), json_output_path=str(scratch / "result.json"), confirm_details=False)
        metrics["final_count"] = summary["final_count"]
        metrics["confirmed_purchase_count"] = summary.get("confirmed_purchase_count", 0)
        metrics["pending_purchase_count"] = summary.get("pending_purchase_count", 0)
        save("completed", summary=summary)
        return 0
    provider_session = None
    try:
        task_timeout = float(cfg.runtime.get("task_timeout_seconds", 600))
        with cancellation_scope(task_timeout) as task_cancel:
            with task_data_session(cfg) as provider_session:
                try:
                    return await asyncio.wait_for(execute(), timeout=task_timeout)
                finally:
                    task_cancel.cancel()
    except Exception as exc:
        metrics["errors"].append({"stage": "task", "error": type(exc).__name__, "reason": str(exc)[:150] if isinstance(exc, ValueError) else "任务未完成"})
        save("failed", errors=metrics["errors"])
        log.error("ecom failed: %s", str(exc)[:150] if isinstance(exc, ValueError) else type(exc).__name__)
        return 1
    finally:
        if callable(getattr(vision, "aclose", None)):
            await vision.aclose()
        metrics["vision"] = vision.metrics
        metrics["provider_stats"] = provider_session.stats() if provider_session is not None else {"onebound": None, "alphashop_mcp": None}
        metrics["elapsed_seconds"] = time.monotonic() - started
        atomic_json(scratch / "metrics.json", metrics)
