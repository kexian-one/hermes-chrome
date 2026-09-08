from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from runtime_support import atomic_json


HEADERS = ["判定ID", "系统结果", "目标商品", "货源商品", "货源ID", "SKU ID", "货源规格", "人工判断", "错配原因", "可采购", "备注"]
MATCH_LABELS = {"正确": True, "同款": True, "错误": False, "非同款": False}
PURCHASE_LABELS = {"可采购": True, "不可采购": False}
GROUP_LABELS = {"final": "推荐", "pending": "待确认", "rejected": "未推荐"}
FROZEN_COLUMNS = ("系统结果", "目标商品", "货源商品", "货源ID", "SKU ID", "货源规格")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _display(value: Any) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _identity_text(value: Any) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text else ""


def _frozen_values(target: dict, candidate: dict, entry: dict) -> dict[str, str]:
    sku = candidate.get("selected_sku") or {}
    return {"系统结果": GROUP_LABELS[entry["group"]], "目标商品": str(target.get("title") or ""), "货源商品": str(candidate.get("title") or ""), "货源ID": entry["offer_id"], "SKU ID": entry["sku_id"], "货源规格": str(sku.get("sku_name") or "")}


def _read_snapshot(path: Path) -> dict:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("反馈对应的商品快照不存在或无法读取，请使用原反馈表") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("反馈对应的商品快照格式错误")
    content = {key: value for key, value in snapshot.items() if key != "dataset_id"}
    if not snapshot.get("dataset_id") or snapshot["dataset_id"] != _digest(content):
        raise ValueError("反馈对应的商品快照不完整或已变化")
    return snapshot


def _preserve_existing(path: Path, snapshot_path: Path, dataset_id: str) -> bool:
    if not path.exists() and not snapshot_path.exists():
        return False
    existing = _read_snapshot(snapshot_path)
    if existing["dataset_id"] != dataset_id:
        raise ValueError("已有反馈文件对应不同的冻结快照，请使用新的输出文件名")
    return path.exists()


def export_feedback(result: dict, path: Path) -> dict:
    path = Path(path)
    candidates, rows, seen = [], [], set()
    target = copy.deepcopy(result.get("target") or {})
    ranking = []
    for group, label in (("final", "推荐"), ("pending", "待确认"), ("rejected", "未推荐")):
        for candidate in result.get(group) or []:
            sku = candidate.get("selected_sku") or {}
            offer_id = str(candidate.get("num_iid") or candidate.get("offerId") or "")
            sku_id = str(sku.get("sku_id") or "")
            pair = (offer_id, sku_id)
            if not offer_id or pair in seen:
                continue
            seen.add(pair)
            candidates.append(copy.deepcopy(candidate))
            row_id = "r" + _digest([target, pair])[:24]
            rows.append({"row_id": row_id, "offer_id": offer_id, "sku_id": sku_id, "group": group})
            if group == "final":
                ranking.append({"offer_id": offer_id, "sku_id": sku_id})
    snapshot = {"target": target, "candidates": candidates, "rows": rows, "predicted_ranking": ranking}
    snapshot["dataset_id"] = _digest(snapshot)
    snapshot_path = path.with_suffix(".snapshot.json")
    if path.exists() or snapshot_path.exists():
        previous = _read_snapshot(snapshot_path)
        if previous["dataset_id"] != snapshot["dataset_id"]:
            path = path.with_name(path.stem + "-" + snapshot["dataset_id"][:10] + path.suffix)
            snapshot_path = path.with_suffix(".snapshot.json")
        if _preserve_existing(path, snapshot_path, snapshot["dataset_id"]):
            return {"feedback_path": str(path), "snapshot_path": str(snapshot_path), "rows": len(rows), "preserved": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(snapshot_path, snapshot)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        for candidate, entry in zip(candidates, rows):
            fields = _frozen_values(target, candidate, entry)
            fields = {key: _identity_text(value) if key in {"货源ID", "SKU ID"} else _display(value) for key, value in fields.items()}
            writer.writerow([entry["row_id"], *(fields[key] for key in FROZEN_COLUMNS), "", "", "", ""])
    return {"feedback_path": str(path), "snapshot_path": str(snapshot_path), "rows": len(rows), "preserved": False}


def import_feedback(path: Path, output: Path | None = None) -> dict:
    path = Path(path)
    snapshot = _read_snapshot(path.with_suffix(".snapshot.json"))
    dataset_id = snapshot["dataset_id"]
    entries = {row["row_id"]: row for row in snapshot["rows"]}
    candidates = {(str(candidate.get("num_iid") or candidate.get("offerId") or ""), str((candidate.get("selected_sku") or {}).get("sku_id") or "")): candidate for candidate in snapshot["candidates"]}
    if len(entries) != len(snapshot["rows"]) or len(candidates) != len(snapshot["candidates"]):
        raise ValueError("冻结快照包含重复商品身份")
    judgments, seen = [], set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(HEADERS).issubset(reader.fieldnames):
            raise ValueError("反馈表缺少必要列")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("反馈表存在重复列名")
        for row in reader:
            if None in row:
                raise ValueError("反馈表列数错误，请使用原反馈表")
            if not any(row.values()):
                continue
            key = (row.get("判定ID") or "").strip()
            if key not in entries or key in seen:
                raise ValueError("反馈判定ID不存在或重复，请使用原反馈表")
            seen.add(key)
            entry = entries[key]
            candidate = candidates.get((entry["offer_id"], entry["sku_id"]))
            if candidate is None or entry["group"] not in GROUP_LABELS:
                raise ValueError("冻结快照缺少判定对应的商品身份")
            frozen = _frozen_values(snapshot["target"], candidate, entry)
            for column, expected in frozen.items():
                exported = _identity_text(expected) if column in {"货源ID", "SKU ID"} else _display(expected)
                if row.get(column) not in {expected, exported}:
                    raise ValueError(f"反馈表的{column}与冻结商品身份不一致，请勿编辑身份列或单独排序部分列")
            match = (row.get("人工判断") or "").strip()
            purchase = (row.get("可采购") or "").strip()
            if match not in {*MATCH_LABELS, "", "未验证", "待确认"}:
                raise ValueError("人工判断请填写 正确、错误 或 未验证")
            if purchase not in {*PURCHASE_LABELS, "", "未验证", "待确认"}:
                raise ValueError("可采购请填写 可采购、不可采购 或 待确认")
            if match not in MATCH_LABELS and purchase not in PURCHASE_LABELS:
                continue
            judgments.append({"offer_id": entry["offer_id"], "sku_id": entry["sku_id"], "match": MATCH_LABELS.get(match), "purchasable": PURCHASE_LABELS.get(purchase), "reason": (row.get("错配原因") or "").strip(), "note": (row.get("备注") or "").strip()})
    if not judgments:
        raise ValueError("尚无人工判定，空白反馈不会作为正确样本")
    case = {"id": dataset_id, "target": snapshot["target"], "candidates": snapshot["candidates"], "expected_matches": [{"offer_id": j["offer_id"], "sku_id": j["sku_id"]} for j in judgments if j["match"] is True], "judgments": judgments, "labels_complete": False, "predicted_ranking": snapshot["predicted_ranking"], "label_source": "human_feedback"}
    execution = path.parent / ".ecom-scratch" / "metrics.json"
    warnings = []
    if execution.is_file():
        try:
            metrics = json.loads(execution.read_text(encoding="utf-8"))
            if not isinstance(metrics, dict):
                raise ValueError("metrics must be an object")
            case["execution"] = {k: metrics[k] for k in ("elapsed_seconds", "model", "target_category", "vision", "retrieval") if k in metrics}
        except (OSError, ValueError):
            warnings.append("运行指标无法读取，已仅导入经过身份校验的人工标签")
    payload = {"schema_version": 2, "cases": [case], "source": str(path)}
    destination = output or path.with_suffix(".labels.json")
    atomic_json(Path(destination), payload)
    return {"output": str(destination), "reviewed_pairs": len(judgments), "matched": len(case["expected_matches"]), "labels_complete": False, "warnings": warnings}


def main() -> None:
    parser = argparse.ArgumentParser(description="导出、导入人工SKU反馈，空白项不视为正确；不调用平台")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("result", type=Path)
    export.add_argument("--output", type=Path, required=True)
    ingest = commands.add_parser("import")
    ingest.add_argument("feedback", type=Path)
    ingest.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = export_feedback(json.loads(args.result.read_text(encoding="utf-8")), args.output) if args.command == "export" else import_feedback(args.feedback, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
