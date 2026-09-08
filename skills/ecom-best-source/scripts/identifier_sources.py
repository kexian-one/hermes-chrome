from __future__ import annotations

from typing import Any

from product_identifiers import normalize_identifiers


def identifiers_from_data(data: dict[str, Any], source: str) -> list[dict[str, str]]:
    existing = data.get("identifiers") or []
    entries = [existing] if isinstance(existing, dict) else list(existing) if isinstance(existing, list) else []
    level_keys = ("barcode_level", "gtin_level", "identifier_level", "barcode_scope", "gtin_scope", "identifier_scope")
    level = next((data[key] for key in level_keys if data.get(key)), "unknown")
    for fields in (data, data.get("attributes")):
        if not isinstance(fields, dict):
            continue
        field_level = next((fields[key] for key in level_keys if fields.get(key)), level)
        for key in ("barcode", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "upc"):
            value = fields.get(key)
            if isinstance(value, dict):
                entries.append({"level": value.get("level") or value.get("scope") or field_level, "source": source + "." + key, **value})
            elif value not in (None, ""):
                entries.append({"value": str(value), "level": field_level, "evidence": str(value), "source": source + "." + key})
    for prop in data.get("props") or []:
        if isinstance(prop, dict) and prop.get("name") in {"条码", "条形码", "商品条码", "GTIN", "EAN", "UPC"}:
            entries.append({"value": prop.get("value"), "level": prop.get("level") or level, "evidence": str(prop.get("value") or ""), "source": source + ".props"})
    return normalize_identifiers(entries)
