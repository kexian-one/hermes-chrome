from __future__ import annotations

import re
import unicodedata
from typing import Any


def validate_gtin(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if raw.startswith("-"):
        return None
    digits = re.sub(r"[\s-]", "", raw)
    if not re.fullmatch(r"(?:[0-9]{8}|[0-9]{12}|[0-9]{13}|[0-9]{14})", digits) or not int(digits):
        return None
    total = sum(int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(digits[:-1])))
    return digits if (10 - total % 10) % 10 == int(digits[-1]) else None


def normalize_identifiers(entries: Any, *, source: str = "") -> list[dict[str, str]]:
    if not isinstance(entries, list):
        return []
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("readable") is False:
            continue
        value = validate_gtin(entry.get("value"))
        if value is None:
            continue
        level = entry.get("level") if entry.get("level") in {"unit", "case"} else "unknown"
        evidence = str(entry.get("evidence") or entry.get("value") or "").strip()
        origin = str(source or entry.get("source") or "unknown")
        if origin.startswith("image:"):
            raw_evidence = entry.get("evidence")
            if not isinstance(raw_evidence, str) or not raw_evidence.strip():
                continue
            compact = re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", raw_evidence))
            if not re.search(r"(?<![0-9])" + value + r"(?![0-9])", compact):
                continue
        key = (value.zfill(14), level, origin)
        if key not in seen:
            result.append({"value": value, "level": level, "evidence": evidence, "source": origin})
            seen.add(key)
    return result


def compare_identifiers(target: Any, candidate: Any) -> dict:
    targets, candidates = normalize_identifiers(target), normalize_identifiers(candidate)
    matches, conflicts, ambiguous = [], [], []
    for level in ("unit", "case"):
        left = {entry["value"].zfill(14) for entry in targets if entry["level"] == level}
        right = {entry["value"].zfill(14) for entry in candidates if entry["level"] == level}
        if len(left) > 1 or len(right) > 1:
            ambiguous.append(level)
            continue
        if not left or not right:
            continue
        comparison = {"level": level, "target": next(iter(left)), "candidate": next(iter(right))}
        (matches if left == right else conflicts).append(comparison)
    return {"status": "conflict" if conflicts else "matched" if matches else "unknown", "matches": matches, "conflicts": conflicts, "ambiguous_levels": ambiguous}
