"""Offline replay of explicitly labelled SKU fixtures; never calls providers."""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from agent.ecom_modules import load_scripts

load_scripts()
from sourcing_rules import run_pipeline
from runtime_support import atomic_json


def percentile(values: list[float], p: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def evaluate(cases: list[dict]) -> dict:
    if not cases:
        raise ValueError("Dataset must contain labelled cases")
    reports, durations = [], []
    tp = fp = fn = unjudged = 0
    top1, top3, observed_top1, observed_top3 = [], [], [], []
    categories = defaultdict(lambda: Counter(tp=0, fp=0, fn=0))
    error_reasons = Counter()
    observed_counts = Counter(tp=0, fp=0, fn=0, unjudged=0, recommended=0, cases=0)
    observed_categories = defaultdict(lambda: Counter(tp=0, fp=0, fn=0))
    recommended = 0
    for case in cases:
        if not isinstance(case.get("expected_matches"), list):
            raise ValueError("Every case needs an explicit expected_matches list, including negative cases")
        expected = {(str(row["offer_id"]), str(row["sku_id"])) for row in case["expected_matches"]}
        complete = case.get("labels_complete", True) is True
        judgments = case.get("judgments") or []
        positives = {(str(j["offer_id"]), str(j["sku_id"])) for j in judgments if j.get("match") is True}
        negatives = {(str(j["offer_id"]), str(j["sku_id"])) for j in judgments if j.get("match") is False}
        if positives & negatives or expected & negatives:
            raise ValueError("Conflicting human labels for the same offer/SKU")
        expected |= positives
        started = time.perf_counter()
        result = run_pipeline({"target": case["target"], "candidates": case["candidates"], "config": case.get("config") or {"output": {"target_count": 6}}})
        durations.append((time.perf_counter() - started) * 1000)
        ranking = [(str(row["num_iid"]), str(row["selected_sku"]["sku_id"])) for row in result["final"]]
        actual = set(ranking)
        recommended += len(actual)
        false_positives = actual - expected if complete else actual & negatives
        missing = expected - actual
        unknown = set() if complete else actual - expected - negatives
        tp += len(actual & expected)
        fp += len(false_positives)
        fn += len(missing)
        unjudged += len(unknown)
        def rank_measure(order: list[tuple[str, str]], count: int) -> int | None:
            chosen = set(order[:count])
            if chosen & expected:
                return 1
            if not chosen:
                return 0 if expected else None
            return 0 if complete or chosen <= negatives else None
        for count, values in ((1, top1), (3, top3)):
            value = rank_measure(ranking, count)
            if value is not None:
                values.append(value)
        if isinstance(case.get("predicted_ranking"), list):
            observed = [(str(row["offer_id"]), str(row["sku_id"])) for row in case["predicted_ranking"]]
            observed_set = set(observed)
            observed_fp = observed_set - expected if complete else observed_set & negatives
            observed_unknown = set() if complete else observed_set - expected - negatives
            observed_counts.update(tp=len(observed_set & expected), fp=len(observed_fp), fn=len(expected - observed_set), unjudged=len(observed_unknown), recommended=len(observed_set), cases=1)
            observed_category = str(case["target"].get("category") or (case["target"].get("attributes") or {}).get("category") or "未知品类")
            observed_categories[observed_category].update(tp=len(observed_set & expected), fp=len(observed_fp), fn=len(expected - observed_set))
            for count, values in ((1, observed_top1), (3, observed_top3)):
                value = rank_measure(observed, count)
                if value is not None:
                    values.append(value)
        category = str(case["target"].get("category") or (case["target"].get("attributes") or {}).get("category") or "未知品类")
        categories[category].update(tp=len(actual & expected), fp=len(false_positives), fn=len(missing))
        for judgment in judgments:
            if judgment.get("match") is False:
                error_reasons[judgment.get("reason") or "未分类"] += 1
        reports.append({"id": case.get("id"), "false_positive": sorted(false_positives), "false_negative": sorted(missing), "unjudged_recommendations": sorted(unknown), "pending": len(result["pending"]), "labels_complete": complete})
    def quality(counts, first, three, grouped):
        correct, incorrect, missed = counts["tp"], counts["fp"], counts["fn"]
        reviewed_precision = correct / (correct + incorrect) if correct + incorrect else None
        return {
            "cases": counts["cases"], "true_positive": correct, "false_positive": incorrect,
            "false_negative": missed, "unjudged_recommendations": counts["unjudged"],
            "recommended_pairs": counts["recommended"], "reviewed_recommendations": correct + incorrect,
            "review_coverage": (correct + incorrect) / counts["recommended"] if counts["recommended"] else None,
            "precision": reviewed_precision, "reviewed_precision": reviewed_precision,
            "recall": correct / (correct + missed) if correct + missed else None,
            "recall_basis": "explicitly labelled positives within the frozen candidate set",
            "top1_accuracy": sum(first) / len(first) if first else None, "top1_evaluated_cases": len(first),
            "top3_hit_rate": sum(three) / len(three) if three else None, "top3_evaluated_cases": len(three),
            "by_category": dict(grouped),
        }
    replay = quality(dict(tp=tp, fp=fp, fn=fn, unjudged=unjudged, recommended=recommended, cases=len(cases)), top1, top3, categories)
    observed_report = quality(observed_counts, observed_top1, observed_top3, observed_categories)
    return {
        "boundary": "offline rule replay at the configured recommendation limit; partial labels score reviewed pairs only; observed_ranking scores recorded output, never reruns search/models",
        "metric_scope": "top-level metrics are current_rule_replay; use observed_ranking for recorded task accuracy",
        **replay, "rule_replay": replay, "observed_ranking": observed_report,
        "human_error_reasons": dict(error_reasons),
        "rule_ms_p50": percentile(durations, .5), "rule_ms_p95": percentile(durations, .95), "details": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.dataset.read_text(encoding="utf-8-sig"))["cases"])
    atomic_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
