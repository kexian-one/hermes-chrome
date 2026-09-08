# 数据流转 Schema

本文定义 `ecom-best-source` 内部数据结构。所有字段都必须来自 JD、1688 API/MCP、浏览器页面或用户输入；不得编造。

## target

```json
{
  "title": "JD 商品完整标题",
  "jd_url": "JD/B2B URL",
  "item_id": "JD 商品 ID",
  "main_image_url": "JD 主图 URL",
  "image_urls": ["JD 商品图 URL"],
  "brand": "品牌主名",
  "brand_aliases": ["品牌别名"],
  "category": "品类",
  "variant": ["颜色/口味/规格变体词"],
  "spec": "75g",
  "form": "液体",
  "jd_price": 0.0,
  "selected_sku": "页面已选 SKU",
  "buy_multiple": 1,
  "buy_multi_mode": "hard | soft | none"
}
```

JD/B2B target 字段优先来自浏览器 MCP 登录态页面。用户给普通 `item.jd.com/<skuId>.html` 时，只用该链接提取 `skuId`，实际价格采集统一打开 B2B 详情 URL。`jd_product.py` 静态 HTML 结果只补浏览器缺失的 `title`、`item_id`、`main_image_url`、`image_urls`，不能覆盖浏览器拿到的 `brand`、`selected_sku`、`price`/`jd_price`、`buy_multiple`。

最终 CSV 里的 `商品金额(元)` 采用实际采购量对应的 SKU 报价和单位换算；`总进货价(元)` 与 `到货总成本(元)` 为商品金额加已确认整单运费、额外税费，减已确认优惠。必需成本未知时总成本留空。`利润率` 仅在 `target.jd_price_tax_included=true` 且价格、数量与完整到货成本明确时按 `(target.jd_price * target.buy_multiple - 到货总成本) / (target.jd_price * target.buy_multiple)` 计算，不含销售平台费用。

发票能力、回头率、响应率不再是 `final` 的顶层字段或打分维度；接口原始返回若包含这些信息，只能留在 `detail` / `seller_info` 原始详情里。

## candidate

```json
{
  "num_iid": "1688 offer id",
  "title": "1688 商品标题",
  "price": 0.0,
  "sales": 0,
  "pic_url": "1688 主图",
  "detail_url": "https://detail.1688.com/offer/<id>.html",
  "sources": ["text", "image"],
  "detail": {
    "unit": "瓶/箱/件",
    "min_num": 1,
    "num": 100,
    "props": [],
    "skus": {
      "sku": [
        {
          "name": "黑色 75g",
          "price": 0.0,
          "quantity": 10
        }
      ]
    }
  },
  "seller_info": {
    "star": 4.8,
    "tpyear": 8
  }
}
```

## sourcing_rules.py input

```json
{
  "target": {},
  "candidates": [],
  "config": {
    "weights": {
      "price": 0.7142857143,
      "composite_service": 0.2857142857
    },
    "output": {"target_count": 6}
  }
}
```

## sourcing_rules.py output

```json
{
  "status": "ok | 召回不足 | 无供给",
  "target": {},
  "final": [
    {
      "num_iid": "1688 offer id",
      "title": "1688 商品标题",
      "link": "https://detail.1688.com/offer/<id>.html",
      "price": 0.0,
      "unitPrice": 0.0,
      "compositeScore": 4.8,
      "MOQ": 3,
      "skuMatchLevel": "完全一致",
      "score": 0.0,
      "score_breakdown": {
        "price": 100.0,
        "composite_service": 96.0
      },
      "recommendationLevel": "首选",
      "warnings": []
    }
  ],
  "rejected": [],
  "rejected_reasons": {},
  "stats": {}
}
```

## 新增证据字段（schema version 2）

- target.attributes/user_attributes：标准属性和用户明确要求，后者优先；require_vision默认true。
- detail.skus.sku[]：sku_id、properties_name、price、quantity（可null）、sku_image_url、price_tiers、vision_attributes、vision。
- selected_sku：绑定SKU、属性、单价、MOQ、库存、order_quantity、purchase_total、图片核验。
- match_evidence：status(matched/unknown/mismatch)、differences、missing、matched。
- detail.fetched_at：取数时间；超过60秒需刷新，失败待确认。
- result.pending：完整待确认列表，CSV在B9后显示最多6条。status新增“部分完成”。
- manifest：schema_version=2、final_count、pending_count、csv_sha256、top3。metrics/checkpoint保存在隐藏工作目录。
- 用户未给数量时沿用页面明确数量或1；包装替代时purchase_total按实际向上取整数量计算。
- target.destination：用户明确的省/市/区或详细收货地区，不能猜测；jd_price_tax_included 表示有依据的京东含税价口径。
- selected_sku.price_evidence：阶梯价、报价/销售单位、实际计价数量、原始库存/MOQ及换算依据。
- purchase：status 为 confirmed/pending/unavailable；components 含 merchandise/shipping/tax/discount，未知使用 null；landed_total/landed_unit 为已确认到货成本，missing/reasons/evidence 保留依据。
- shipping_quote/tax_quote/discount_quote：只采用适用 SKU、采购量、地区及条件相符的明确报价；普通 post_fee 不能视为整单运费。
- identifiers：value、level(unit/case/unknown)、evidence、source；仅有效且同层级的 GTIN 参与辅助比较。多 SKU 不借用其他行或未绑定的商品总条码。
- selected_sku.identifier_comparison：matched/conflict/unknown；冲突待确认，一致不覆盖图文差异或未知。
- manifest 追加 confirmed_purchase_count/pending_purchase_count、feedback_path/feedback_snapshot_path。反馈 CSV 空白不产生标签，人工导入绑定冻结身份。
