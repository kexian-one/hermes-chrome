from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any


BRAND_ALIAS_EXPAND: dict[str, list[str]] = {
    "红鸟": ["红鸟", "RED BIRD", "红鸟RED BIRD", "庄臣红鸟", "庄臣"],
    "绿劲": ["绿劲", "立白绿劲", "绿劲妈妈"],
    "海天": ["海天", "海天酱油", "海天味业"],
    "立白": ["立白", "Liby", "立白集团"],
    "白猫": ["白猫", "白猫日化"],
    "宝洁": ["宝洁", "P&G", "PROCTER", "Procter & Gamble"],
    "联合利华": ["联合利华", "Unilever"],
    "蓝月亮": ["蓝月亮", "Bluemoon", "蓝月亮Bluemoon"],
    "雕牌": ["雕牌", "纳爱斯雕牌", "纳爱斯"],
    "金龙鱼": ["金龙鱼", "益海嘉里", "金龙鱼Arawana"],
    "鲁花": ["鲁花", "鲁花花生油"],
    "李锦记": ["李锦记", "Lee Kum Kee", "李锦记LKK"],
    "厨邦": ["厨邦", "厨邦酱油", "美味鲜"],
    "千禾": ["千禾", "千禾味业"],
    "太太乐": ["太太乐", "太太乐鸡精"],
    "心相印": ["心相印", "心相印纸巾"],
    "维达": ["维达", "Vinda", "维达Vinda"],
    "清风": ["清风", "Breeze", "清风Breeze"],
    "舒肤佳": ["舒肤佳", "Safeguard", "舒肤佳Safeguard"],
    "高露洁": ["高露洁", "Colgate", "高露洁Colgate"],
    "老街口": ["老街口", "老街口瓜子"],
    "三只松鼠": ["三只松鼠", "Three Squirrels"],
    "良品铺子": ["良品铺子", "BESTORE"],
    "百草味": ["百草味", "Be&Cheery"],
    "卫龙": ["卫龙", "卫龙美味"],
    "盼盼": ["盼盼", "盼盼食品"],
    "洽洽": ["洽洽", "洽洽食品", "ChaCheer"],
    "佳洁士": ["佳洁士", "Crest", "佳洁士Crest"],
    "黑人": ["黑人", "Darlie", "黑人牙膏"],
    "飘柔": ["飘柔", "Rejoice", "飘柔Rejoice"],
    "海飞丝": ["海飞丝", "Head & Shoulders", "海飞丝Head&Shoulders"],
    "潘婷": ["潘婷", "Pantene", "潘婷Pantene"],
    "清扬": ["清扬", "CLEAR"],
}

CATEGORY_WORDS = [
    "皮鞋油", "果蔬清洁", "餐具净", "鞋蜡", "鞋膏", "鞋油",
    "洗洁精", "洗衣液", "洗衣粉", "柔顺剂",
    "卷纸", "抽纸", "纸巾", "湿巾",
    "牙膏", "牙刷", "漱口水",
    "洗发水", "护发素", "沐浴露",
    "奶粉", "辅食",
    "尿不湿", "纸尿裤",
    "饮料", "矿泉水", "酸奶",
    "方便面", "泡面", "桶面", "袋面", "速食面", "拉面",
    "魔芋爽", "素毛肚", "辣条",
    "饼干", "坚果", "糖果",
    "酱油", "料酒", "蚝油", "食用油", "调味料", "醋",
]

FORM_WORDS = [
    "液体", "固体", "膏体", "油膏", "粉末", "颗粒",
    "蜡", "鞋膏", "鞋蜡", "绵羊油", "片", "胶囊",
    "桶装", "袋装", "碗装", "杯装", "盒装",
]

COLOR_WORDS = [
    "黑色", "白色", "红色", "蓝色", "黄色", "绿色",
    "灰色", "粉色", "紫色", "棕色", "金色", "银色",
    "卡其", "藏青", "自然色", "透明",
    "黑", "白", "红", "蓝",
]

FLAVOR_WORDS = [
    "柚子金桔", "柚子柠檬", "金桔柠檬", "青柠薄荷",
    "柚子", "柠檬", "薄荷", "金桔",
    "红烧牛肉", "香辣牛肉", "酸辣牛肉", "葱香排骨",
    "红烧", "酸辣", "原味", "麻辣", "香辣", "番茄", "牛肉", "蜂蜜",
    "海盐", "焦糖", "抹茶", "草莓", "蓝莓", "巧克力",
]

SPEC_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|mg|ml|L|克|毫升|斤|两|片|粒|包|瓶|箱|双|个|寸|cm|mm|m)",
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
            parts.append(self.variant[0])
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
        category=_extract_first(clean, CATEGORY_WORDS),
        variant=_extract_variant(clean),
        spec=_extract_spec(clean),
        form=_extract_first(clean, FORM_WORDS),
    )


def normalize_title(title: str) -> str:
    text = DECOR_BRACKETS_RE.sub("", title or "")
    noise = ["新品", "热卖", "正品", "官方", "包邮", "旗舰店"]
    for word in noise:
        text = text.replace(word, "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_brand(title: str, known_brand: str | None) -> tuple[str | None, list[str]]:
    if known_brand:
        brand = known_brand.strip()
        return brand, list(BRAND_ALIAS_EXPAND.get(brand, [brand]))
    for brand, aliases in BRAND_ALIAS_EXPAND.items():
        if title.startswith(brand) or brand in title:
            return brand, list(aliases)
    match = BRAND_CN_EN_RE.match(title)
    if match:
        cn = match.group(1)
        en = match.group(2).strip()
        return cn, _uniq([cn, en, f"{cn}{en.title()}"])
    match = BRAND_CN_RE.match(title)
    if match:
        prefix = match.group(1)
        fallback = prefix[:2]
        if re.fullmatch(r"[\u4e00-\u9fff]+", fallback):
            return fallback, [fallback]
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
    variants = []
    color = _extract_first(title, COLOR_WORDS)
    flavor = _extract_first(title, FLAVOR_WORDS)
    for value in (color, flavor):
        if value and value not in variants:
            variants.append(value)
    return variants


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
