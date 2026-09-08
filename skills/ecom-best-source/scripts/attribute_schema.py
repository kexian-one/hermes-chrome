from __future__ import annotations

import re
from typing import Any

BRAND_ALIAS_EXPAND: dict[str, list[str]] = {
    "红鸟": ["红鸟", "RED BIRD", "红鸟RED BIRD", "庄臣红鸟"],
    "绿劲": ["绿劲", "立白绿劲", "绿劲妈妈"],
    "海天": ["海天", "海天酱油", "海天味业"],
    "立白": ["立白", "Liby", "立白集团"],
    "白猫": ["白猫", "白猫日化"],
    "宝洁": ["宝洁", "P&G", "PROCTER", "Procter & Gamble"],
    "联合利华": ["联合利华", "Unilever"],
    "蓝月亮": ["蓝月亮", "Bluemoon", "蓝月亮Bluemoon"],
    "雕牌": ["雕牌", "纳爱斯雕牌"],
    "金龙鱼": ["金龙鱼", "金龙鱼Arawana"],
    "可口可乐": ["可口可乐", "Coca-Cola"],
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
    "饮料", "矿泉水", "酸奶", "汽水", "牛奶",
    "方便面", "泡面", "桶面", "袋面", "速食面", "拉面",
    "魔芋爽", "素毛肚", "辣条",
    "饼干", "坚果", "糖果",
    "酱油", "料酒", "蚝油", "食用油", "调味料", "醋",
]

CATEGORY_ALIAS_GROUPS = {
    "洗发水": ["洗发水", "洗发露", "洗头膏", "洗发膏"],
    "沐浴露": ["沐浴露", "沐浴乳"],
    "护发素": ["护发素", "护发乳"],
    "纸尿裤": ["纸尿裤", "尿不湿"],
    "方便面": ["方便面", "泡面", "速食面"],
    "鞋油": ["鞋油", "皮鞋油"],
}


def compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


_BRAND_NAMES = {compact(alias): canonical for canonical, aliases in BRAND_ALIAS_EXPAND.items() for alias in aliases}
_CATEGORY_NAMES = {compact(alias): canonical for canonical, aliases in CATEGORY_ALIAS_GROUPS.items() for alias in aliases}


def canonical_name(key: str, value: Any) -> str | None:
    text = compact(value)
    names = _BRAND_NAMES if key == "brand" else _CATEGORY_NAMES
    return names.get(text, text) or None


def category_aliases(category: str) -> list[str]:
    canonical = canonical_name("category", category)
    return CATEGORY_ALIAS_GROUPS.get(canonical, [category])


def brand_aliases(brand: str) -> list[str]:
    canonical = canonical_name("brand", brand)
    return BRAND_ALIAS_EXPAND.get(canonical, [brand])


def required_attribute_keys(attributes: dict[str, Any]) -> set[str]:
    required = {"brand", "category", "pack_count", "packaging"}
    category = canonical_name("category", attributes.get("category")) or ""
    if category == "抽纸":
        required.update({"layers", "sheets", "size"})
    elif category == "卷纸":
        required.add("layers")
    elif category == "湿巾":
        required.update({"sheets", "size"})
    food = {"饮料", "汽水", "酸奶", "牛奶", "方便面", "桶面", "袋面", "饼干", "坚果", "糖果", "魔芋爽", "素毛肚", "辣条", "辅食", "零食", "咖啡", "茶"}
    liquid_or_weight = food | {"矿泉水", "奶粉", "酱油", "料酒", "蚝油", "食用油", "调味料", "醋", "洗发水", "沐浴露", "护发素", "洗衣液", "洗衣粉", "洗洁精", "柔顺剂", "鞋油", "牙膏", "漱口水"}
    if category in liquid_or_weight:
        required.add("net_content")
    if category in food:
        required.add("flavor")
    return required
