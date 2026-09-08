from __future__ import annotations

import copy
import csv
import json

import pytest

from sourcing_feedback import export_feedback, import_feedback
from sourcing_rules import run_pipeline
from scripts.evaluate_sourcing import evaluate
from scripts.sourcing_metrics import summarize
from agent.builtin_tools import _jd_product_ready


def result():
    return run_pipeline({"target": {}, "candidates": [{"num_iid": "1", "title": "=1+1", "price": 5}, {"num_iid": "2", "title": "另一款", "price": 6}]})


def edit(path, **values):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers, rows = reader.fieldnames, list(reader)
    rows[0].update(values)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, headers)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def test_blank_feedback_never_becomes_a_positive_label(tmp_path):
    path = tmp_path / "反馈.csv"
    export_feedback(result(), path)
    with pytest.raises(ValueError, match="尚无人工判定"):
        import_feedback(path)
    assert "'=1+1" in path.read_text(encoding="utf-8-sig")


def test_feedback_round_trip_preserves_partial_labels_and_original_ranking(tmp_path):
    path = tmp_path / "反馈.csv"
    export_feedback(result(), path)
    edit(path, **{"人工判断": "错误", "错配原因": "错口味", "可采购": "不可采购"})
    summary = import_feedback(path)
    cases = json.loads((tmp_path / "反馈.labels.json").read_text(encoding="utf-8"))["cases"]
    report = evaluate(cases)
    assert summary["reviewed_pairs"] == 1
    assert report["false_positive"] == 1
    assert report["unjudged_recommendations"] == 1
    assert report["top1_accuracy"] == 0
    assert report["top3_hit_rate"] is None
    assert report["observed_ranking"]["top1_accuracy"] == 0
    assert report["human_error_reasons"] == {"错口味": 1}


def test_feedback_snapshot_identity_and_duplicate_rows_are_checked(tmp_path):
    path = tmp_path / "反馈.csv"
    export_feedback(result(), path)
    rows = edit(path, **{"人工判断": "正确"})
    with path.open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, rows[0].keys()).writerow(rows[0])
    with pytest.raises(ValueError, match="重复"):
        import_feedback(path)
    snapshot = path.with_suffix(".snapshot.json")
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["target"]["title"] = "changed"
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="快照"):
        import_feedback(path)


def test_reexport_does_not_overwrite_human_feedback(tmp_path):
    path = tmp_path / "反馈.csv"
    original = result()
    export_feedback(original, path)
    edit(path, **{"人工判断": "正确"})
    before = path.read_bytes()
    assert export_feedback(original, path)["preserved"]
    changed = copy.deepcopy(original)
    changed["target"]["title"] = "new target"
    alternative = export_feedback(changed, path)["feedback_path"]
    assert alternative != str(path)
    assert path.read_bytes() == before


def test_evaluation_uses_real_six_result_limit_and_known_labels_only():
    case = {"target": {}, "candidates": [{"num_iid": str(i), "title": str(i), "price": i + 1} for i in range(8)], "expected_matches": [{"offer_id": "7", "sku_id": ""}]}
    report = evaluate([case])
    assert report["false_negative"] == 1
    assert report["false_positive"] == 6
    assert report["top3_hit_rate"] == 0


def test_browser_can_finish_stable_single_sku_without_optional_price():
    product = {"title": "商品", "item_id": "123", "main_image_url": "image", "readiness": {"document_ready": True, "loading": False, "sku_bound": True, "sku_option_count": 0}}
    assert not _jd_product_ready(product, 1)
    assert _jd_product_ready(product, 2)
    product["readiness"]["loading"] = True
    assert not _jd_product_ready(product, 3)
    product["readiness"].update(loading=False, sku_bound=False, sku_option_count=4)
    assert not _jd_product_ready(product, 3)


def test_metrics_keep_accuracy_unknown_without_labels_and_cost_unknown_without_pricing(tmp_path):
    scratch = tmp_path / "one" / ".ecom-scratch"
    scratch.mkdir(parents=True)
    metrics = {"elapsed_seconds": 5, "model": "qwen3.8-flash", "target_category": "饼干", "final_count": 2, "confirmed_purchase_count": 1, "pending_purchase_count": 1, "vision": {"calls": 3, "verified_skus": 2}, "retrieval": {"candidate_count": 30, "detail_attempts": 10, "stop_reason": "time_budget_exhausted"}, "provider_stats": {"onebound": {"new_calls": {"item_search": 1}, "cost_yuan": None}}}
    (scratch / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    report = summarize(tmp_path)
    assert report["human_feedback"]["quality"] is None
    assert report["providers"]["known_configured_cost_yuan"] is None
    assert report["providers"]["providers_with_unknown_cost"] == 1
    assert report["vision_calls_per_recommended_supplier"] == 1.5
    assert report["funnel"]["pending_purchase_suppliers"] == 1
    assert report["by_model_and_category"][0]["category"] == "饼干"


def test_recorded_accuracy_is_not_replaced_by_improved_rule_replay(tmp_path):
    case = {"id": "recorded", "target": {"title": "样例牌草莓饼干100g×6袋", "brand": "样例牌", "category": "饼干"},
            "candidates": [{"num_iid": "good", "title": "样例牌饼干", "detail": {"skus": [{"sku_id": "good", "properties_name": "草莓100g×6袋", "price": 5, "quantity": 100}]}},
                           {"num_iid": "bad", "title": "样例牌饼干", "detail": {"skus": [{"sku_id": "bad", "properties_name": "巧克力100g×6袋", "price": 4, "quantity": 100}]}}],
            "expected_matches": [{"offer_id": "good", "sku_id": "good"}], "labels_complete": False,
            "judgments": [{"offer_id": "good", "sku_id": "good", "match": True}, {"offer_id": "bad", "sku_id": "bad", "match": False}],
            "predicted_ranking": [{"offer_id": "bad", "sku_id": "bad"}, {"offer_id": "good", "sku_id": "good"}]}
    report = evaluate([case])
    assert report["rule_replay"]["precision"] == 1
    assert report["observed_ranking"]["precision"] == .5
    assert report["observed_ranking"]["review_coverage"] == 1
    directory = tmp_path / "task"
    directory.mkdir()
    (directory / "review.labels.json").write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    metrics = summarize(tmp_path)
    assert metrics["human_feedback"]["quality"]["reviewed_precision"] == .5
    assert metrics["human_feedback"]["rule_replay"]["precision"] == 1
    case["judgments"].pop()
    partial = evaluate([case])["observed_ranking"]
    assert partial["reviewed_precision"] == 1
    assert partial["review_coverage"] == .5
    assert partial["unjudged_recommendations"] == 1


def test_optional_broken_metrics_does_not_lose_valid_human_feedback(tmp_path):
    path = tmp_path / "反馈.csv"
    export_feedback(result(), path)
    edit(path, **{"人工判断": "正确"})
    scratch = tmp_path / ".ecom-scratch"
    scratch.mkdir()
    (scratch / "metrics.json").write_text("{", encoding="utf-8")
    imported = import_feedback(path)
    assert imported["matched"] == 1
    assert imported["warnings"]
    payload = json.loads((tmp_path / "反馈.labels.json").read_text(encoding="utf-8"))
    assert "execution" not in payload["cases"][0]
