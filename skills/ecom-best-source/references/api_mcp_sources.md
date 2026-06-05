# API/MCP 数据源路由

## 优先级

1. Alphashop/1688 MCP
   - 文搜: `keywordSearchProduct`
   - 图搜: `imageSearchProduct`
   - 详情: `productDetailQuery`
2. Onebound 1688 API
   - 文搜: `item_search`
   - 图搜: `item_search_img`
   - 详情: `item_get`
   - 店铺: `seller_info`
3. 浏览器 1688 页面
   - 只做兜底，不作为默认路径。

## 凭证位置

凭证存放在项目根 `config.yaml` 的 `ecom_best_source` 段，不写入 `SKILL.md`、reference 或输出报告。

```yaml
ecom_best_source:
  data_source: hybrid
  onebound:
    base: https://api-gw.onebound.cn/1688
    key: ...
    secret: ...
  alphashop_mcp:
    endpoint: https://mcp.alphashop.cn/sse
    ak: ...
    sk: ...
  ai:
    matcher:
      base_url: ...
      model: ...
      api_key: ...
      enabled: true
```

脚本读取配置时使用 `scripts/ecom_config.py`。只允许输出 mask 后的状态，不要把明文 key 写入日志、报告或聊天回复。

## 可执行脚本

- `scripts/ecom_config.py --status`: 检查数据源配置，脱敏输出。
- `scripts/keyword_builder.py --title ...`: 从 JD 标题构造 target、query、extra_queries。
- `scripts/fetch_candidates.py --query ... [--image-url ...]`: 真实调用 Onebound / Alphashop MCP / hybrid，输出归一化候选。
- `scripts/sourcing_rules.py --input candidates.json`: 对候选应用最终筛选规则。

## MCP 能替代的浏览器动作

| 原浏览器动作 | 替代方式 | 备注 |
|---|---|---|
| 打开 1688 搜索页填关键词 | MCP `keywordSearchProduct` 或 Onebound `item_search` | 可多 query、多页召回 |
| 上传 JD 主图以图搜款 | MCP `imageSearchProduct` 或 Onebound `item_search_img` | 失败只影响图搜通道 |
| 进 1688 详情页抓 SKU/价格/库存 | MCP `productDetailQuery` 或 Onebound `item_get` | 详情用于库存、起批、规格救回 |
| 抓店铺评分/年限 | Onebound `seller_info` | Alphashop MCP 缺店铺详情时按中性分 |

## 推荐召回策略

- 主 query: `brand category first_variant spec`
- brand-only query: 品牌词单独搜，用于补强品牌命中。
- 英文/别名 query: 只对英文别名跑 1 到数页。
- 图搜: JD 主图优先，再取详情页前几张商品图；接口免费时可放宽张数。
- 合并去重: `num_iid`、`offerId` 或详情 URL 中 offer id。

## MCP 模式建议

- 文搜每个 query 可拉更多页。
- 图搜可用多张 JD 图片。
- 详情可拉更大的 top_k。
- 对“标题品牌命中但规格/形态缺失”的候选，可以用详情 SKU/属性救回。
- 不因为 MCP 便宜就放宽最终规则；最终仍走 `final_filter_rules.md`。

## Onebound 保留价值

- 店铺 `seller_info`。
- MCP 失败时 fallback。
- MCP 详情缺字段时补 `item_get`。

## 字段归一化

每个候选至少归一为:

```json
{
  "num_iid": "1688 offer id",
  "title": "候选标题",
  "price": 0.0,
  "sales": 0,
  "pic_url": "主图",
  "detail_url": "https://detail.1688.com/offer/<id>.html",
  "sources": ["text", "image"],
  "detail": {
    "unit": "箱/件/瓶等",
    "min_num": 1,
    "num": 100,
    "props": [],
    "skus": {"sku": []}
  },
  "seller_info": {}
}
```
