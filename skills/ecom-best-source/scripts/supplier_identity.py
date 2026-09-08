from __future__ import annotations

import re
from typing import Any


def supplier_key(candidate: dict[str, Any]) -> str:
    detail = candidate.get("detail") or {}
    sellers = [candidate.get("seller_info") or {}, detail.get("seller_info") or {}, candidate.get("sellerInfo") or {}, detail.get("sellerInfo") or {}]
    for source in [*sellers, candidate, detail]:
        for key in ("sid", "seller_id", "sellerId", "supplierId", "supplier_id", "memberId", "member_id"):
            if source.get(key):
                return "id:" + str(source[key]).strip()
    for source in [candidate, detail, *sellers]:
        for key in ("shopName", "shop_name", "companyName", "sellerName"):
            if source.get(key):
                return "name:" + re.sub(r"\s+", "", str(source[key])).casefold()
    for seller in sellers:
        for key in ("title", "nick"):
            if seller.get(key):
                return "name:" + re.sub(r"\s+", "", str(seller[key])).casefold()
    return "offer:" + str(candidate.get("num_iid") or candidate.get("offerId") or candidate.get("detail_url") or candidate.get("link") or id(candidate))
