from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import uuid
from copy import deepcopy
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from agent.ecom_modules import load_scripts
from agent.llm_client import LLMClient

load_scripts()
from product_match import (
    ATTRIBUTE_KEYS, attribute_field, compare_attributes, normalize_value, required_attribute_keys,
    row_attribute_evidence, sku_rows, target_attributes,
)
from runtime_support import cache_key, read_cache, write_cache
from supplier_identity import supplier_key as _supplier_key
from product_identifiers import normalize_identifiers, compare_identifiers

PROMPT_VERSION = "sku-evidence-v5"
IMAGE_FACTS_PROMPT = """只识别这一张商品图片中的客观事实。没有目标商品、候选商品或预期属性，不得猜测它应当是什么商品。
返回 {product_visible: true|false, readability: clear|partial|unreadable, attributes: {brand,category,series,flavor,sugar_content,fat_content,color,form,net_content,pack_count,packaging,packaging_variant,pack_structure,model,layers,sheets,size,sales_unit,gift_count}, evidence: {字段名: 图片清晰可见的原文}, identifiers: [{value: 条码下方完整数字字符串, level: unit|case|unknown, readable: true|false, evidence: 图片原文及包装层级文字依据}]}。
attributes仅记录图片实际可见且有原文证据的字段；不清楚就省略，不得补写口味、数量、型号。包装装潢颜色不是SKU颜色。糖/脂肪含量与口味分别记录；包装材料和补充装/礼盒等包装版本分别记录。不要按陈列道具数量猜包装件数，赠品不计入基础件数。
条码仅抄录清晰可见的完整GTIN/EAN/UPC数字，模糊缺位不要猜。unit指单件商品标签，case指明确外箱标签；层级不明确填unknown，不得凭位数推断层级。有效条码不证明真伪，也不单独证明同款。
"""
EXTRACT_PROMPT = """识别指定商品的实际销售 SKU。结合已选规格、商品资料和已确认属于该 SKU 的图片标签提取属性。
返回 {attributes: {brand,category,series,flavor,sugar_content,fat_content,color,form,net_content,pack_count,packaging,packaging_variant,pack_structure,model,layers,sheets,size,sales_unit,gift_count}, evidence: {字段名: 图片可见原文或输入资料原文}, conflicts: []}。
每个非空属性必须有可核对的证据。不要按陈列道具数量猜包装件数；不要把总重量当单件重量；不要把多规格商品标题中的多个选项合并。输入存在明确冲突时写入 conflicts。
color 只表示商品本身的 SKU 颜色，不提取包装装潢颜色。糖含量、脂肪含量与口味分别提取。纸品读取层数、抽数、尺寸；包装层级示例6袋*2盒，pack_count为基础商品总件数12，赠品数量单独记录gift_count。
focus_fields 是待补证据的字段，图片都属于同一个目标 SKU；不要用某张图上的促销赠品改变基础规格。
"""
COMPARE_PROMPT = """核验京东目标 SKU 与 1688 候选 SKU 是否同款。按 image_roles 识别每张图片属于 target 还是 candidate；分别读取口味、糖/脂肪含量、净含量、包装数量、系列、形态、型号等标签。
返回 {attributes: {brand,category,series,flavor,sugar_content,fat_content,color,form,net_content,pack_count,packaging,packaging_variant,pack_structure,model,layers,sheets,size,sales_unit,gift_count}, evidence: {字段名: 候选图片可见原文}, status: matched|mismatch|unknown, differences: [{field,target,candidate}], reason: 简短依据}。
attributes和evidence只描述候选图片实际可见的属性，不得把输入的SKU文字冒充图片原文。candidate是绑定到当前SKU的文字证据，文字已明确的字段不要求再次出现在图片上；例如整箱件数由SKU规格证明，单罐图证明品牌、口味、净含量。综合图文证据一致且有实际图片正面证据时才matched。任一关键属性冲突为mismatch；缺少决定同款的证据或图片无法确认商品为unknown。
status、attributes和differences必须一致，matched时differences必须为空。包装换版不能仅因装潢颜色不同判错；袋数不能根据道具推断。禁止把商品通用主图当成特定 SKU 已选图的证据。
color只描述商品SKU颜色；品类按目标分类粒度描述，纸品核对层数、抽数、尺寸；gift_count不参与基础同款判断。focus_fields是本次需补充证据的字段。
"""
_VISUAL_IDENTITY_FIELDS = {
    "series", "flavor", "sugar_content", "fat_content", "color", "form",
    "net_content", "packaging", "packaging_variant", "pack_structure", "model", "layers", "sheets", "size",
}


def validated_attributes(payload: dict) -> dict:
    attrs, evidence = payload.get("attributes"), payload.get("evidence")
    if not isinstance(attrs, dict) or not isinstance(evidence, dict):
        raise ValueError("视觉输出缺少属性证据")
    result = {}
    routed_evidence = {}
    for key, value in attrs.items():
        if key not in ATTRIBUTE_KEYS or value in (None, ""):
            continue
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise ValueError("视觉属性缺少可核对的证据")
        routed_key = attribute_field(key, value)
        normalized = normalize_value(routed_key, value)
        if normalized is None:
            raise ValueError("视觉属性值无效")
        if routed_key in result and result[routed_key] != normalized:
            raise ValueError("视觉包装属性相互矛盾")
        result[routed_key] = normalized
        routed_evidence[routed_key] = evidence[key]
    payload["evidence"] = routed_evidence
    return result


def _image_urls(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for image in value:
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.strip():
            url = url.strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("http://"):
                url = "https://" + url[7:]
            if url not in result:
                result.append(url)
    return result


def _target_images(target: dict) -> list[str]:
    images = _image_urls(target.get("main_image_url"))
    images.extend(_image_urls(target.get("label_image_urls")))
    images.extend(_image_urls(target.get("barcode_image_urls")))
    if target.get("image_urls_scope") == "selected_sku" or target.get("image_urls_verified") is True:
        images.extend(_image_urls(target.get("image_urls")))
    return list(dict.fromkeys(images))[:3]


def _candidate_images(item: dict, row: dict, single: bool) -> list[str]:
    images = []
    for key in ("sku_image_url", "image", "pic_url", "label_image_urls", "barcode_image_urls", "sku_image_urls", "image_urls"):
        images.extend(_image_urls(row.get(key)))
    if single:
        images.extend(_image_urls(item.get("pic_url")))
        images.extend(_image_urls((item.get("detail") or {}).get("item_imgs")))
    return list(dict.fromkeys(images))[:3]


class ProductVision:
    def __init__(self, llm: LLMClient, cache_dir: Path, concurrency: int = 3, timeout: float = 45, image_loader=None, *, image_cache_seconds: float = 3600) -> None:
        self.llm, self.cache_dir = llm, cache_dir
        self.limit = asyncio.Semaphore(max(1, min(8, concurrency)))
        self.timeout = timeout
        self.image_cache_seconds = max(0, image_cache_seconds)
        self.image_loader = image_loader or self._cached_image
        self.images: dict[str, asyncio.Task] = {}
        self.facts: dict[str, asyncio.Task] = {}
        self._http_client: httpx.AsyncClient | None = None
        self.metrics = {"calls": 0, "cache_hits": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "verified_skus": 0, "budget_deferred": 0, "missing_images": 0, "supplemental_calls": 0, "response_retries": 0, "image_downloads": 0, "image_cache_hits": 0, "ocr_calls": 0, "ocr_cache_hits": 0, "deterministic_comparisons": 0}

    async def aclose(self) -> None:
        tasks = [task for task in [*self.images.values(), *self.facts.values()] if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _cached_image(self, url: str) -> tuple[str, str]:
        index = self.cache_dir / "images" / (cache_key("image-url-v1", url) + ".json")
        cached = read_cache(index, self.image_cache_seconds)
        if isinstance(cached, dict) and re.fullmatch(r"[0-9a-f]{64}", str(cached.get("sha256") or "")) and cached.get("mime") in {"image/jpeg", "image/png", "image/webp"}:
            path = self.cache_dir / "images" / (cached["sha256"] + ".bin")
            try:
                if path.stat().st_size <= 8 * 1024 * 1024:
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() == cached["sha256"]:
                        self.metrics["image_cache_hits"] += 1
                        return "data:" + cached["mime"] + ";base64," + base64.b64encode(content).decode("ascii"), cached["sha256"]
            except OSError:
                pass
        data_uri, digest = await self._download_image(url)
        mime, encoded = data_uri[5:].split(";base64,", 1)
        content = base64.b64decode(encoded, validate=True)
        index.parent.mkdir(parents=True, exist_ok=True)
        destination = index.parent / (digest + ".bin")
        temporary = destination.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        write_cache(index, {"sha256": digest, "mime": mime})
        return data_uri, digest

    async def _download_image(self, url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        domains = ("360buyimg.com", "jdimg.com", "alicdn.com", "1688.com", "taobaocdn.com")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not any(host == d or host.endswith("." + d) for d in domains):
            raise ValueError("商品图片地址不在支持的图片域名内")
        async with self.limit:
            if self._http_client is None:
                self._http_client = httpx.AsyncClient(timeout=15, follow_redirects=False)
            self.metrics["image_downloads"] += 1
            async with self._http_client.stream("GET", url) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";")[0]
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("不支持的图片类型")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("商品图片超过8MB")
        return f"data:{mime};base64," + base64.b64encode(content).decode("ascii"), hashlib.sha256(content).hexdigest()

    async def _image(self, url: str) -> tuple[str, str]:
        if url not in self.images:
            self.images[url] = asyncio.create_task(self.image_loader(url))
        else:
            self.metrics["image_cache_hits"] += 1
        try:
            return await self.images[url]
        except Exception:
            self.images.pop(url, None)
            raise

    async def _request(self, prompt: str, data: dict, images: list[str], *, force_refresh: bool = False) -> dict:
        loaded = await asyncio.gather(*(self._image(url) for url in images))
        key = cache_key(PROMPT_VERSION, [getattr(self.llm, "_model", ""), prompt, data, [value[1] for value in loaded]])
        path = self.cache_dir / (key + ".json")
        cached = None if force_refresh else read_cache(path, 86400)
        if cached is not None:
            self.metrics["cache_hits"] += 1
            if prompt == IMAGE_FACTS_PROMPT:
                self.metrics["ocr_cache_hits"] += 1
            return cached
        async with self.limit:
            self.metrics["calls"] += 1
            if prompt == IMAGE_FACTS_PROMPT:
                self.metrics["ocr_calls"] += 1
            response = await asyncio.wait_for(self.llm.structured(prompt, data, [value[0] for value in loaded]), timeout=self.timeout)
        self.metrics["input_tokens"] += int(response.usage.get("prompt_tokens") or 0)
        self.metrics["output_tokens"] += int(response.usage.get("completion_tokens") or 0)
        if response.finish_reason != "stop":
            raise ValueError("视觉输出不完整")
        payload = json.loads(response.text or "")
        if not isinstance(payload, dict):
            raise ValueError("视觉输出不是对象")
        payload["attributes"] = validated_attributes(payload)
        write_cache(path, payload)
        return payload

    async def _image_facts(self, url: str) -> dict:
        _, digest = await self._image(url)
        key = cache_key("image-facts-v1", [getattr(self.llm, "_model", ""), digest])
        if key not in self.facts:
            self.facts[key] = asyncio.create_task(self._request(IMAGE_FACTS_PROMPT, {"operation": "read_product_label", "image_sha256": digest}, [url]))
        else:
            self.metrics["ocr_cache_hits"] += 1
        try:
            facts = deepcopy(await self.facts[key])
        except Exception:
            self.facts.pop(key, None)
            raise
        facts["usable"] = facts.get("product_visible") is True and facts.get("readability") in {"clear", "partial"}
        if facts.get("conflicts") or facts.get("differences"):
            raise ValueError("独立图片事实输出包含相互矛盾的结论")
        facts["identifiers"] = normalize_identifiers([entry for entry in (facts.get("identifiers") or []) if isinstance(entry, dict) and entry.get("readable") is True], source="image:" + url)
        facts["image_url"], facts["image_sha256"] = url, digest
        return facts

    async def _target_payload(self, product: dict, data: dict, images: list[str]) -> dict:
        facts = await asyncio.gather(*(self._image_facts(url) for url in images))
        product["identifiers"] = normalize_identifiers([*(product.get("identifiers") or []), *(entry for fact in facts for entry in fact["identifiers"])])
        usable = [fact for fact in facts if fact["usable"]]
        if not usable:
            return await self._request(EXTRACT_PROMPT, data, images)
        attributes, evidence, conflicts = {}, {}, []
        aliases = [str(product.get("brand") or ""), *(product.get("brand_aliases") or [])]
        for fact in usable:
            conflicts.extend(compare_attributes(attributes, fact["attributes"], aliases)["differences"])
            attributes.update(fact["attributes"])
            evidence.update(fact["evidence"])
        return {"attributes": attributes, "evidence": evidence, "conflicts": conflicts}

    async def extract_target(self, product: dict) -> dict:
        images = _target_images(product)
        data = {k: product.get(k) for k in ("title", "selected_sku", "brand", "attributes", "user_attributes")}
        existing = target_attributes(product)
        aliases = [str(existing.get("brand") or product.get("brand") or ""), *(product.get("brand_aliases") or [])]
        payload = await self._target_payload(product, data, images[:1])
        inferred = payload["attributes"]
        evidence = dict(payload["evidence"])
        evidence_by_call = [{"image_urls": images[:1], "evidence": evidence}]
        conflicts = compare_attributes(existing, inferred, aliases)["differences"]
        reported_conflicts = bool(payload.get("conflicts"))
        merged = {**inferred, **existing}
        missing = required_attribute_keys(merged) - {k for k, v in merged.items() if v not in (None, "")}
        labels = _image_urls(product.get("label_image_urls")) + _image_urls(product.get("barcode_image_urls"))
        needs_identifier_label = not product.get("identifiers") and bool(set(labels).intersection(images[1:]))
        if (missing or needs_identifier_label) and len(images) > 1 and not conflicts and not reported_conflicts:
            self.metrics["supplemental_calls"] += 1
            payload = await self._target_payload(product, {**data, "focus_fields": sorted(missing)}, images)
            extra = payload["attributes"]
            conflicts.extend(compare_attributes(inferred, extra, aliases)["differences"])
            conflicts.extend(compare_attributes(existing, extra, aliases)["differences"])
            inferred = {**inferred, **extra}
            evidence = {**evidence, **payload["evidence"]}
            evidence_by_call.append({"image_urls": images, "evidence": payload["evidence"]})
            reported_conflicts = bool(payload.get("conflicts"))
        if conflicts or reported_conflicts:
            product.setdefault("target_errors", []).append("目标文字与图片属性冲突，需要人工核实")
        product["attributes"] = {**inferred, **existing}
        product["vision"] = {"evidence": evidence, "evidence_by_call": evidence_by_call, "conflicts": conflicts, "model_reported_conflicts": reported_conflicts, "image_urls": evidence_by_call[-1]["image_urls"], "model": getattr(self.llm, "_model", "")}
        return product

    @staticmethod
    def _compare_payload(payload: dict, expected: dict, text_attrs: dict, target: dict, previous: dict | None = None) -> dict:
        status = payload.get("status")
        if status not in {"matched", "mismatch", "unknown"}:
            raise ValueError("视觉输出缺少有效匹配状态")
        aliases = [str(expected.get("brand") or target.get("brand") or ""), *(target.get("brand_aliases") or [])]
        allow_pack = bool(target.get("allow_pack_substitution"))
        image_attrs = payload["attributes"]
        image_conflicts = compare_attributes((previous or {}).get("image_attributes") or {}, image_attrs, aliases)["differences"]
        image_attrs = {**((previous or {}).get("image_attributes") or {}), **image_attrs}
        evidence = {**((previous or {}).get("evidence") or {}), **payload["evidence"]}
        comparison = compare_attributes(expected, image_attrs, aliases, allow_pack)
        text_conflicts = compare_attributes(text_attrs, image_attrs, aliases)["differences"]
        differences = comparison["differences"] + text_conflicts + image_conflicts
        declared = payload.get("differences", [])
        if not isinstance(declared, list) or payload.get("conflicts"):
            raise ValueError("视觉差异格式无效")
        declared_differences = []
        for difference in declared:
            if not isinstance(difference, dict) or difference.get("field") not in {*ATTRIBUTE_KEYS, "image"}:
                raise ValueError("视觉差异字段无效")
            field = difference["field"]
            if field == "gift_count":
                continue
            if any(difference.get(key) in (None, "") for key in ("target", "candidate")):
                raise ValueError("视觉差异缺少属性值")
            routed_field = attribute_field(field, difference["candidate"])
            if routed_field != attribute_field(field, difference["target"]):
                raise ValueError("视觉差异混合了不同包装属性")
            field = routed_field
            difference = {**difference, "field": field}
            if field != "image":
                if not compare_attributes({field: difference["target"]}, {field: difference["candidate"]}, aliases, allow_pack)["differences"]:
                    continue
                for known, reported in ((expected.get(field), difference["target"]), (image_attrs.get(field), difference["candidate"])):
                    if known not in (None, "") and compare_attributes({field: known}, {field: reported}, aliases)["differences"]:
                        raise ValueError("视觉属性与差异相互矛盾")
            declared_differences.append(difference)
        if status == "matched" and declared_differences:
            raise ValueError("视觉匹配状态与差异相互矛盾")
        differences.extend(declared_differences)
        fused = {**image_attrs, **text_attrs}
        fused_comparison = compare_attributes(expected, fused, aliases, allow_pack)
        visible_matches = set(comparison["matched"])
        identity_fields = _VISUAL_IDENTITY_FIELDS.intersection(expected)
        if len(identity_fields) > 1:
            identity_fields.discard("packaging")
        positive_evidence = bool(visible_matches.intersection(identity_fields or {"brand", "category"}))
        if differences:
            status = "mismatch"
        elif status == "mismatch":
            raise ValueError("视觉不匹配结论缺少差异证据")
        elif status == "matched" and (fused_comparison["missing"] or not positive_evidence):
            status = "unknown"
        return {"status": status, "differences": differences, "missing": fused_comparison["missing"], "evidence": evidence, "reason": payload.get("reason"), "image_attributes": image_attrs, "positive_evidence": positive_evidence}

    async def verify_candidates(self, target: dict, candidates: list[dict], max_pairs: int = 20) -> dict[str, int]:
        expected = target_attributes(target)
        target_images = _target_images(target)
        suppliers: dict[str, list[dict]] = {}
        for item in candidates:
            rows = sku_rows(item.get("detail") or {})
            for index, row in enumerate(rows):
                text_row = {k: v for k, v in row.items() if k not in {"vision", "vision_attributes"}}
                evidence = row_attribute_evidence(item, text_row, len(rows) > 1, target)
                attrs = evidence["attributes"]
                if evidence.get("conflicts"):
                    row.pop("vision_attributes", None)
                    row["vision"] = {"status": "unknown", "reason": "SKU文字证据冲突，需要核实", "code": "text_conflict", "attribute_conflicts": evidence["conflicts"]}
                    continue
                preliminary = compare_attributes(expected, attrs, target.get("brand_aliases"), bool(target.get("allow_pack_substitution")))
                if preliminary["status"] == "mismatch":
                    continue
                images = _candidate_images(item, row, len(rows) == 1)
                signature = cache_key(PROMPT_VERSION, [getattr(self.llm, "_model", ""), expected, attrs, target.get("brand_aliases"), target.get("allow_pack_substitution"), target_images, images, str(item.get("num_iid") or item.get("offerId")), str(row.get("sku_id") or row.get("skuId") or index)])
                previous = row.get("vision") or {}
                previous_attempts = int(previous.get("attempt_count") or 0) if previous.get("verification_signature") == signature else 0
                if previous.get("verification_signature") == signature and previous.get("completed") and (not previous.get("retryable") or previous_attempts >= 2):
                    continue
                row.pop("vision_attributes", None)
                if not target_images or not images:
                    self.metrics["missing_images"] += 1
                    row["vision"] = {"status": "unknown", "reason": "缺少目标商品图片" if not target_images else "缺少可绑定到 SKU 的商品图片", "code": "missing_image"}
                    continue
                job = {"item": item, "row": row, "index": index, "images": images, "attrs": attrs, "signature": signature, "attempt_count": previous_attempts + 1, "priority": (row.get("quantity") == 0, len(preliminary["missing"]), index), "attribute_sources": evidence.get("sources") or {}}
                suppliers.setdefault(_supplier_key(item), []).append(job)
        groups = [deque(sorted(jobs, key=lambda job: job["priority"])) for jobs in suppliers.values()]
        groups.sort(key=lambda group: group[0]["priority"])
        jobs = []
        while groups:
            next_groups = []
            for group in groups:
                jobs.append(group.popleft())
                if group:
                    next_groups.append(group)
            groups = next_groups
        budget = max(0, int(max_pairs))
        for job in jobs[budget:]:
            self.metrics["budget_deferred"] += 1
            job["row"]["vision"] = {"status": "unknown", "reason": "本轮图片核验预算未覆盖", "code": "budget_deferred"}
            if job["attempt_count"] > 1:
                job["row"]["vision"].update({"verification_signature": job["signature"], "attempt_count": job["attempt_count"] - 1, "completed": True, "retryable": True})

        async def verify(job: dict) -> None:
            item, row, attrs = job["item"], job["row"], job["attrs"]
            self.metrics["verified_skus"] += 1
            data = {"target": expected, "candidate": attrs, "candidate_attribute_sources": job["attribute_sources"], "offer_id": str(item.get("num_iid") or item.get("offerId")), "sku_id": str(row.get("sku_id") or row.get("skuId") or job["index"]), "allow_pack_substitution": bool(target.get("allow_pack_substitution")), "brand_aliases": target.get("brand_aliases") or []}
            async def compare(images: list[str], roles: list[str], focus: list[str], previous: dict | None = None) -> dict:
                request_data = {**data, "image_roles": roles, "focus_fields": focus}
                for attempt in range(2):
                    try:
                        payload = await self._request(COMPARE_PROMPT, request_data, images, force_refresh=attempt > 0)
                        return self._compare_payload(payload, expected, attrs, target, previous)
                    except ValueError:
                        if attempt:
                            raise
                        self.metrics["response_retries"] += 1
                raise AssertionError("unreachable")
            try:
                result = None
                image_facts = []
                used_images = []
                labels = set(_image_urls(row.get("label_image_urls")) + _image_urls(row.get("barcode_image_urls")))
                for image_url in job["images"]:
                    if result and result["status"] == "mismatch":
                        break
                    if result and result["status"] == "matched" and image_url not in labels:
                        continue
                    fact = await self._image_facts(image_url)
                    image_facts.append(fact)
                    used_images.append(image_url)
                    row["identifiers"] = normalize_identifiers([*(row.get("identifiers") or []), *fact["identifiers"]])
                    if fact["usable"]:
                        result = self._compare_payload({"attributes": fact["attributes"], "evidence": fact["evidence"], "status": "matched", "differences": [], "reason": "按当前SKU文字逐字段核对独立图片事实"}, expected, attrs, target, result)
                explicitly_unreadable = image_facts and all(fact.get("product_visible") is False or fact.get("readability") == "unreadable" for fact in image_facts)
                if result is None and explicitly_unreadable:
                    result = self._compare_payload({"attributes": {}, "evidence": {}, "status": "unknown", "differences": [], "reason": "图片看不清或没有可识别的商品"}, expected, attrs, target)
                used_pair_model = False
                if (result is None or result["status"] == "unknown") and not explicitly_unreadable:
                    used_images = [target_images[0], job["images"][0]]
                    result = await compare(used_images, ["target", "candidate"], [], result)
                    used_pair_model = True
                if result["status"] == "unknown" and used_pair_model and (len(target_images) > 1 or len(job["images"]) > 1):
                    self.metrics["supplemental_calls"] += 1
                    used_images = target_images + job["images"]
                    focus = result["missing"] or sorted(_VISUAL_IDENTITY_FIELDS.intersection(expected))
                    result = await compare(used_images, ["target"] * len(target_images) + ["candidate"] * len(job["images"]), focus, result)
                if not used_pair_model:
                    self.metrics["deterministic_comparisons"] += 1
                result["identifier_comparison"] = compare_identifiers(target.get("identifiers"), row.get("identifiers"))
                if result["identifier_comparison"]["status"] == "conflict" and result["status"] == "matched":
                    result["status"] = "unknown"
                    result["reason"] = "同包装层级条码冲突，需要核实"
                result["image_fact_sources"] = [{"url": fact["image_url"], "sha256": fact["image_sha256"]} for fact in image_facts]
                row["vision_attributes"] = result.pop("image_attributes")
                row["vision"] = {**result, "code": "verified" if result["status"] != "unknown" else "insufficient_evidence", "image_urls": used_images, "attribute_sources": job["attribute_sources"], "model": getattr(self.llm, "_model", ""), "prompt_version": PROMPT_VERSION, "verification_signature": job["signature"], "attempt_count": job["attempt_count"], "completed": True}
            except Exception as exc:
                self.metrics["errors"] += 1
                row["vision"] = {"status": "unknown", "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__, "code": "invalid_response" if isinstance(exc, ValueError) else "vision_error", "verification_signature": job["signature"], "attempt_count": job["attempt_count"], "completed": True, "retryable": not isinstance(exc, ValueError)}
        await asyncio.gather(*(verify(job) for job in jobs[:budget]))
        return {"attempted": min(budget, len(jobs)), "remaining": max(0, len(jobs) - budget)}
