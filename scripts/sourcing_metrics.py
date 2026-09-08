from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from scripts.evaluate_sourcing import evaluate, percentile


def summarize(root: Path) -> dict:
    runs, unreadable = [], 0
    for path in root.glob("*/.ecom-scratch/metrics.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("metrics must be an object")
            runs.append(payload)
        except (OSError, ValueError):
            unreadable += 1
    def number(value):
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
        except (TypeError, ValueError):
            return None
    def mapping(value):
        return value if isinstance(value, dict) else {}
    def distribution(values):
        numbers = [parsed for value in values if (parsed := number(value)) is not None]
        return {"count": len(numbers), "p50": percentile(numbers, .5), "p95": percentile(numbers, .95)}
    stages, groups = defaultdict(list), defaultdict(list)
    stops, finals, categories = Counter(), Counter(), Counter()
    provider_calls = Counter()
    provider_cache = Counter(cache_hits=0, coalesced_hits=0)
    known_cost = 0.0
    known_cost_providers = unknown_cost_providers = 0
    for run in runs:
        for name, seconds in mapping(run.get("stages")).items():
            if (parsed := number(seconds)) is not None:
                stages[name].append(parsed)
        category = str(run.get("target_category") or "未知品类")
        categories[category] += 1
        groups[(str(run.get("model") or "unknown"), category)].append(run)
        retrieval = mapping(run.get("retrieval"))
        stops[str(retrieval.get("stop_reason") or "unrecorded")] += 1
        if "final_count" in run:
            finals[str(run["final_count"])] += 1
        for provider, stats in mapping(run.get("provider_stats")).items():
            if not isinstance(stats, dict):
                continue
            for counter in ("cache_hits", "coalesced_hits"):
                provider_cache[counter] += number(stats.get(counter)) or 0
            for operation, count in mapping(stats.get("new_calls")).items():
                if (parsed := number(count)) is not None:
                    provider_calls[provider + "." + operation] += parsed
            cost = number(stats.get("cost_yuan"))
            if cost is None:
                unknown_cost_providers += 1
            else:
                known_cost += cost
                known_cost_providers += 1
    elapsed = distribution(run.get("elapsed_seconds") for run in runs)
    def vision_total(key):
        return sum(number(mapping(run.get("vision")).get(key)) or 0 for run in runs)
    labels, bad_labels = {}, 0
    for path in root.glob("*/*.labels.json"):
        try:
            for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
                try:
                    evaluate([case])
                    labels[str(case["id"])] = case
                except (ValueError, KeyError, TypeError, AttributeError):
                    bad_labels += 1
        except (OSError, ValueError, KeyError, TypeError):
            bad_labels += 1
    evaluation = evaluate(list(labels.values())) if labels else None
    quality = evaluation["observed_ranking"] if evaluation and evaluation["observed_ranking"]["cases"] else None
    judgments = [judgment for case in labels.values() for judgment in case.get("judgments", [])]
    purchase_labels = [j["purchasable"] for j in judgments if isinstance(j.get("purchasable"), bool)]
    total_final = sum(number(run.get("final_count")) or 0 for run in runs)
    report = {"runs": len(runs), "unreadable": unreadable, "exported_runs": sum("final_count" in run for run in runs), "runs_with_errors": sum(bool(run.get("errors")) for run in runs), "elapsed_p50_seconds": elapsed["p50"], "elapsed_p95_seconds": elapsed["p95"], "elapsed_sample_count": elapsed["count"], "stages": {name: {"p50_seconds": percentile(values, .5), "p95_seconds": percentile(values, .95)} for name, values in stages.items()}, "vision_calls": vision_total("calls"), "vision_cache_hits": vision_total("cache_hits"), "vision_errors": vision_total("errors"), "vision_input_tokens": vision_total("input_tokens"), "vision_output_tokens": vision_total("output_tokens"), "vision_calls_per_recommended_supplier": vision_total("calls") / total_final if total_final else None, "stop_reasons": dict(stops), "final_count_distribution": dict(finals), "category_runs": dict(categories), "funnel": {"retrieved_candidates": sum(number(mapping(run.get("retrieval")).get("candidate_count")) or 0 for run in runs), "detail_attempts": sum(number(mapping(run.get("retrieval")).get("detail_attempts")) or 0 for run in runs), "verified_skus": vision_total("verified_skus"), "recommended_suppliers": total_final, "confirmed_purchase_suppliers": sum(number(run.get("confirmed_purchase_count")) or 0 for run in runs), "pending_purchase_suppliers": sum(number(run.get("pending_purchase_count")) or 0 for run in runs)}, "providers": {"calls": dict(provider_calls), "known_configured_cost_yuan": round(known_cost, 6) if known_cost_providers else None, "providers_with_unknown_cost": unknown_cost_providers, "cost_basis": "explicit configuration only; excludes unconfigured model fees and is not a billing statement"}, "by_model_and_category": [{"model": model, "category": category, "runs": len(group), "elapsed_seconds": distribution(run.get("elapsed_seconds") for run in group), "recommended_suppliers": sum(number(run.get("final_count")) or 0 for run in group)} for (model, category), group in groups.items()], "human_feedback": {"labelled_tasks": len(labels), "reviewed_pairs": len(judgments), "unreadable": bad_labels, "purchasable_rate": sum(purchase_labels) / len(purchase_labels) if purchase_labels else None, "purchasable_sample_count": len(purchase_labels), "quality": quality, "quality_scope": "recorded recommendations, reviewed pairs only; see review_coverage", "rule_replay": evaluation["rule_replay"] if evaluation else None, "error_reasons": evaluation["human_error_reasons"] if evaluation else {}}, "quality_boundary": "only explicit human labels measure accuracy; latency and cache counters do not establish SKU correctness"}
    report["providers"].update(provider_cache)
    report["image_processing"] = {key: vision_total(key) for key in ("image_downloads", "image_cache_hits", "ocr_calls", "ocr_cache_hits", "deterministic_comparisons")}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize local sourcing latency and model usage, without network calls")
    parser.add_argument("--outputs", type=Path, default=Path("outputs"))
    args = parser.parse_args()
    print(json.dumps(summarize(args.outputs), ensure_ascii=False, indent=2))
