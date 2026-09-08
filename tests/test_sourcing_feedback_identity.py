from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

import pytest

from agent.ecom_modules import load_scripts

load_scripts()
from sourcing_feedback import export_feedback, import_feedback


def result():
    return {"target": {"title": "目标草莓饼干100g×6袋"}, "final": [
        {"num_iid": "001234567890123456789", "title": "=测试货源", "selected_sku": {"sku_id": "009876543210987654321", "sku_name": "草莓100g×6袋"}},
        {"num_iid": "991234567890123456789", "title": "另一个货源", "selected_sku": {"sku_id": "889876543210987654321", "sku_name": "草莓100g×12袋"}},
    ], "pending": [], "rejected": []}


def read_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def write_rows(path: Path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(rows)


def labelled_rows(path: Path):
    export_feedback(result(), path)
    fields, rows = read_rows(path)
    rows[0]["人工判断"] = "正确"
    return fields, rows


@pytest.mark.parametrize("column", ["系统结果", "目标商品", "货源商品", "货源ID", "SKU ID", "货源规格"])
def test_modified_frozen_display_column_cannot_relabel_original_sku(tmp_path, column):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    rows[0][column] = "另一个商品的值"
    write_rows(path, fields, rows)
    with pytest.raises(ValueError, match="冻结商品身份不一致"):
        import_feedback(path)


def test_swapping_valid_row_ids_cannot_swap_human_labels(tmp_path):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    rows[0]["判定ID"], rows[1]["判定ID"] = rows[1]["判定ID"], rows[0]["判定ID"]
    write_rows(path, fields, rows)
    with pytest.raises(ValueError, match="冻结商品身份不一致"):
        import_feedback(path)


def test_sorting_whole_rows_preserves_the_original_sku_identity(tmp_path):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    write_rows(path, fields, rows[::-1])
    import_feedback(path)
    case = json.loads(path.with_suffix(".labels.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["expected_matches"] == [{"offer_id": "001234567890123456789", "sku_id": "009876543210987654321"}]


@pytest.mark.parametrize("excel_removes_export_quote", [False, True])
def test_long_ids_and_leading_zeros_are_text_and_accept_one_export_quote(tmp_path, excel_removes_export_quote):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    assert rows[0]["货源ID"] == "'001234567890123456789"
    assert rows[0]["SKU ID"] == "'009876543210987654321"
    if excel_removes_export_quote:
        for row in rows:
            for column in ("货源ID", "SKU ID", "货源商品"):
                if row[column].startswith("'"):
                    row[column] = row[column][1:]
    write_rows(path, fields, rows)
    import_feedback(path)
    case = json.loads(path.with_suffix(".labels.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["judgments"][0]["offer_id"] == "001234567890123456789"
    assert case["judgments"][0]["sku_id"] == "009876543210987654321"


@pytest.mark.parametrize("damaged", ["1.23456789012346E+18", "001234567890123460000", "1234567890123456789", ""])
def test_excel_rounding_scientific_notation_and_lost_zeroes_are_rejected(tmp_path, damaged):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    rows[0]["货源ID"] = damaged
    write_rows(path, fields, rows)
    with pytest.raises(ValueError, match="货源ID与冻结商品身份不一致"):
        import_feedback(path)


def test_a_literal_leading_quote_is_not_treated_as_an_export_escape(tmp_path):
    path = tmp_path / "feedback.csv"
    source = result()
    source["final"][0]["title"] = "'真实标题开头的引号"
    export_feedback(source, path)
    fields, rows = read_rows(path)
    rows[0]["人工判断"] = "正确"
    rows[0]["货源商品"] = "真实标题开头的引号"
    write_rows(path, fields, rows)
    with pytest.raises(ValueError, match="冻结商品身份不一致"):
        import_feedback(path)


def test_duplicate_column_names_are_rejected_before_attaching_labels(tmp_path):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([*fields, "人工判断"])
        for row in rows:
            writer.writerow([*(row[field] for field in fields), "错误"])
    with pytest.raises(ValueError, match="重复列名"):
        import_feedback(path)


@pytest.mark.parametrize("corruption", ["changed", "missing", "different_valid_snapshot"])
def test_existing_suffix_snapshot_is_verified_without_overwriting_human_content(tmp_path, corruption):
    path = tmp_path / "feedback.csv"
    original = result()
    export_feedback(original, path)
    changed = copy.deepcopy(original)
    changed["target"]["title"] += "另一目标"
    alternative = Path(export_feedback(changed, path)["feedback_path"])
    fields, rows = read_rows(alternative)
    rows[0]["人工判断"] = "正确"
    rows[0]["备注"] = "保留人工记录"
    write_rows(alternative, fields, rows)
    previous = alternative.read_bytes()
    snapshot = alternative.with_suffix(".snapshot.json")
    if corruption == "changed":
        value = json.loads(snapshot.read_text(encoding="utf-8"))
        value["target"]["title"] = "快照已改"
        snapshot.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    elif corruption == "missing":
        snapshot.unlink()
    else:
        snapshot.write_bytes(path.with_suffix(".snapshot.json").read_bytes())
    with pytest.raises(ValueError, match="快照"):
        export_feedback(changed, path)
    assert alternative.read_bytes() == previous


def test_valid_reexport_keeps_original_and_suffix_human_annotations(tmp_path):
    path = tmp_path / "feedback.csv"
    original = result()
    fields, rows = labelled_rows(path)
    write_rows(path, fields, rows)
    previous = path.read_bytes()
    assert export_feedback(original, path)["preserved"]
    assert path.read_bytes() == previous
    changed = copy.deepcopy(original)
    changed["target"]["title"] += "另一个"
    alternative = Path(export_feedback(changed, path)["feedback_path"])
    fields, rows = read_rows(alternative)
    rows[0]["人工判断"] = "错误"
    write_rows(alternative, fields, rows)
    previous_alternative = alternative.read_bytes()
    assert export_feedback(changed, path)["preserved"]
    assert alternative.read_bytes() == previous_alternative and path.read_bytes() == previous


def test_corrupt_base_snapshot_never_gets_a_false_preserved_result(tmp_path):
    path = tmp_path / "feedback.csv"
    fields, rows = labelled_rows(path)
    write_rows(path, fields, rows)
    before = path.read_bytes()
    snapshot = path.with_suffix(".snapshot.json")
    value = json.loads(snapshot.read_text(encoding="utf-8"))
    value["target"]["title"] = "保持旧dataset_id但篡改快照"
    snapshot.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="快照"):
        export_feedback(result(), path)
    assert path.read_bytes() == before
