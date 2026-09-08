from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


def validate_sourcing_output(path: Path) -> dict | None:
    try:
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 2 or manifest.get("status") not in {"ok", "召回不足", "无供给", "部分完成"}:
            return None
        if Path(manifest["csv_path"]).resolve() != path.resolve():
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("csv_sha256"):
            return None
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"匹配状态", "SKU ID", "规格匹配", "1688链接"}.issubset(reader.fieldnames or []):
                return None
            rows = list(reader)
        qualified = [r for r in rows if r.get("匹配状态") == "合格"]
        if len(qualified) != manifest.get("final_count"):
            return None
        if any(not r.get("1688链接") or r.get("规格匹配") not in {"完全一致", "规格同数量不同"} for r in qualified):
            return None
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        return None
