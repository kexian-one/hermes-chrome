# 数据流转 Schema

本文定义 `ecom-best-source` 内部数据结构。所有字段都必须来自 JD、1688 API/MCP、浏览器页面或用户输入；不得编造。

## target

```json
{
  "title": "JD 商品完整标题",
  "jd_url": "JD/B2B URL",
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
    "output": {"target_count": 5}
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
