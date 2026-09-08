from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any

from attribute_schema import BRAND_ALIAS_EXPAND, CATEGORY_WORDS, category_aliases
from product_match import FLAVORS, FORM_VALUES, normalize_value, parse_attributes


FORM_WORDS = [
    "液体", "固体", "膏体", "油膏", "粉末", "颗粒",
    "蜡", "鞋膏", "鞋蜡", "绵羊油", "片", "胶囊",
    "桶装", "袋装", "碗装", "杯装", "盒装",
]

COLOR_WORDS = [
    "黑色", "白色", "红色", "蓝色", "黄色", "绿色",
    "灰色", "粉色", "紫色", "棕色", "金色", "银色",
    "卡其", "藏青", "自然色", "透明",
]

FLAVOR_WORDS = list(FLAVORS)

SPEC_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kg|公斤|千克|mg|毫克|g|克|ml|毫升|L|升|斤|两)(?![a-zA-Z])",
    re.IGNORECASE,
)
BRAND_CN_EN_RE = re.compile(r"^([\u4e00-\u9fff]{1,6})[（(]([A-Za-z][A-Za-z &]*)[)）]")
BRAND_CN_RE = re.compile(r"^([\u4e00-\u9fff]{2,6})")
DECOR_BRACKETS_RE = re.compile(r"[【〖\[][^】〗\]]*[】〗\]]")


@dataclass
class Keywords:
    brand: str | None = None
    brand_aliases: list[str] = field(default_factory=list)
    category: str | None = None
    variant: list[str] = field(default_factory=list)
    spec: str | None = None
    form: str | None = None

    def to_query(self) -> str:
        parts = []
        if self.brand:
            parts.append(self.brand)
        if self.category:
            parts.append(self.category)
        if self.variant:
            parts.extend(self.variant)
        if self.spec:
            parts.append(self.spec)
        return " ".join(parts)

    def extra_queries(self) -> list[str]:
        out = []
        context = [x for x in [self.category, self.spec] if x]
        if self.brand:
            out.append(" ".join([self.brand, *context]) if context else self.brand)
        for alias in self.brand_aliases:
            if not alias or alias == self.brand:
                continue
            has_ascii_alpha = any(c.isascii() and c.isalpha() for c in alias)
            if has_ascii_alpha and not self.category:
                continue
            out.append(" ".join([alias, *context]) if context else alias)
        seen = set()
        primary = self.to_query()
        return [q for q in out if q != primary and not (q in seen or seen.add(q))]

    def to_target(self, title: str, **extra: Any) -> dict[str, Any]:
        return {
            "title": title,
            "brand": self.brand,
            "brand_aliases": self.brand_aliases,
            "category": self.category,
            "variant": self.variant,
            "spec": self.spec,
            "form": self.form,
            **extra,
        }


def build_keywords(title: str, known_brand: str | None = None) -> Keywords:
    clean = normalize_title(title)
    brand, aliases = _extract_brand(clean, known_brand)
    return Keywords(
        brand=brand,
        brand_aliases=aliases,
        category=normalize_value("category", _extract_first(clean, [alias for category in CATEGORY_WORDS for alias in category_aliases(category)])),
        variant=_extract_variant(clean.replace(brand, "") if brand else clean),
        spec=_extract_spec(clean),
        form=_extract_first(clean, list(FORM_VALUES)),
    )


def normalize_title(title: str) -> str:
    text = re.sub(r"[【】〖〗\[\]]", " ", title or "")
    noise = ["新品", "热卖", "正品", "官方", "包邮", "旗舰店"]
    for word in noise:
        text = text.replace(word, "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_brand(title: str, known_brand: str | None) -> tuple[str | None, list[str]]:
    if known_brand:
        brand = normalize_value("brand", known_brand) or known_brand.strip()
        return brand, list(BRAND_ALIAS_EXPAND.get(brand, [brand]))
    matches = []
    for brand, aliases in BRAND_ALIAS_EXPAND.items():
        for alias in aliases:
            pattern = re.escape(alias)
            if alias.isascii():
                pattern = r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])"
            if hit := re.search(pattern, title, re.I):
                matches.append((hit.start(), -len(alias), brand))
    if matches:
        brand = min(matches)[2]
        return brand, list(BRAND_ALIAS_EXPAND[brand])
    match = BRAND_CN_EN_RE.match(title)
    if match:
        cn = match.group(1)
        en = match.group(2).strip()
        return cn, _uniq([cn, en, f"{cn}{en.title()}"])
    return None, []


def _extract_first(title: str, words: list[str]) -> str | None:
    best: tuple[int, int, str] | None = None
    for word in sorted(set(words), key=len, reverse=True):
        pos = title.find(word)
        if pos < 0:
            continue
        key = (pos, -len(word), word)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def _extract_variant(title: str) -> list[str]:
    attrs = parse_attributes(title)
    return list(dict.fromkeys(attrs[key] for key in ("color", "flavor", "sugar_content", "fat_content") if attrs.get(key)))


def _extract_spec(title: str) -> str | None:
    match = SPEC_RE.search(title)
    if not match:
        return None
    value = match.group(1)
    unit = _normalize_unit(match.group(2))
    return f"{value}{unit}"


def _normalize_unit(unit: str) -> str:
    normalized = unit.lower()
    return {
        "克": "g",
        "毫升": "ml",
        "l": "L",
    }.get(normalized, normalized)


def _uniq(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ecom-best-source keywords from a JD title")
    parser.add_argument("--title", required=True)
    parser.add_argument("--known-brand")
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()
    kw = build_keywords(args.title, args.known_brand)
    text = json.dumps({
        **kw.to_target(args.title),
        "query": kw.to_query(),
        "extra_queries": kw.extra_queries(),
    }, ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
