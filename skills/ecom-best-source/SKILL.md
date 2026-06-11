---
name: ecom-best-source
description: JD/B2B 京东商品找 1688 同款货源、比价、批量找供应商。用于用户说找货源、找同款、1688 比价、哪里进货便宜、给 b2b.jd.com 或 item.jd.com 链接要找可采购货源时。优先用 1688 API/MCP/接口召回和详情，最终筛选只按价格和综合服务分排序，移除发票能力、回头率、响应率；不要依赖外部 huoyuan 文件夹。
requires_browser_mcp: false
---

# 电商找最优货源

目标: 从 JD 或京东万商商品出发，找 1688 可采购同款货源，最终只输出 1 个 CSV。不要依赖外部 `huoyuan`；它只是历史参考，执行时不得读取、导入或调用任何机器上的历史 `huoyuan` 目录。

## 核心原则

- 优先接口/MCP，不优先浏览器点 1688 页面。
- 1688 侧召回、图搜、详情能走 API/MCP 就走 API/MCP；浏览器只做缺工具时的兜底。
- 最后筛选只保留价格和综合服务分。移除发票能力、回头率、响应率三个原维度；原 `50:20` 比例归一后为价格 71.43%、综合服务分 28.57%；综合服务分 <3、入驻 <1 年降为不推荐。
- 普通 `item.jd.com` 如果用户没给目标 SKU/规格，先问清楚；`b2b.jd.com/goods/goods-detail/...` 直接按页面已选 SKU。
- 最终 CSV 必须包含完整 1688 详情链接、价格、起批数/库存判断和 Top 5 推荐。

## 推荐流程

1. 提取 JD 商品信息: title、main_image_url、image_urls、brand、selected_sku、price、buy_multiple。
2. 构造关键词: `brand + category + variant + spec`，不要凭空换原标题里不存在的品类词。
3. 召回候选:
   - 首选 Alphashop/1688 MCP: 文搜、图搜、商品详情。
   - 备选 Onebound/1688 API: `item_search`、`item_search_img`、`item_get`、`seller_info`。
   - 兜底浏览器: 只在没有接口/MCP 或接口失败且用户允许时使用 1688 页面。
4. 统一候选字段后运行原 skill 最终筛选规则: `scripts/sourcing_rules.py`。
5. 只写出一个最终 CSV，并在回复里给 Top 3 摘要。中间 JSON 只作为临时文件使用，不能作为最终产出。

详细数据源路由见 `references/api_mcp_sources.md`。详细筛选规则见 `references/final_filter_rules.md`。数据结构见 `references/data_schema.md`。

## 内置规则脚本

本 skill 的私有密钥放在项目根 `config.yaml` 的 `ecom_best_source` 段。检查配置状态时运行:

在 worker 内执行脚本时，使用内置工具 `run_ecom_script`，不要调用 shell。下面的命令行示例对应:

```json
{"script": "ecom_config.py", "args": ["--status"]}
```

```bash
python skills/ecom-best-source/scripts/ecom_config.py --status
```

该命令只输出脱敏状态，不输出明文 key。后续 API/MCP 脚本应 import `ecom_config.load_ecom_config()`，不要读取外部历史目录。

从 JD / 京东万商 URL 提取目标商品信息:

```bash
python skills/ecom-best-source/scripts/jd_product.py --url "<item.jd.com 或 b2b.jd.com 商品URL>" --output jd_product.json
```

对应 `run_ecom_script`:

```json
{"script": "jd_product.py", "args": ["--url", "<item.jd.com 或 b2b.jd.com 商品URL>", "--output", "jd_product.json"]}
```

输出包含 `title`、`jd_url`、`item_id`、`main_image_url`、`image_urls`。如果页面反爬导致标题/图片为空，改用浏览器 MCP 打开该 URL 后用 JS 读 `document.title`、`og:image`、页面商品图数据；不要截图猜标题。

从 JD 标题构造 target / query:

```bash
python skills/ecom-best-source/scripts/keyword_builder.py --title "<JD标题>" --known-brand "<页面品牌>"
```

召回 1688 候选时运行:

```bash
python skills/ecom-best-source/scripts/fetch_candidates.py --data-source mcp --query "红鸟 鞋油 黑色 75g" --extra-query "红鸟" --extra-query "RED BIRD" --image-url "<JD主图URL>" --output candidates.json
```

当本环境可以执行 Python 时，优先用 skill-local 脚本完成最后筛选，避免 LLM 手算漂移:

```bash
python skills/ecom-best-source/scripts/sourcing_rules.py --input candidates.json --output final.json
```

输入 JSON:

```json
{
  "target": {
    "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
    "brand": "红鸟",
    "brand_aliases": ["红鸟", "RED BIRD", "庄臣红鸟", "庄臣"],
    "category": "鞋油",
    "variant": ["黑色"],
    "spec": "75g",
    "form": "液体",
    "buy_multiple": 40,
    "buy_multi_mode": "hard"
  },
  "candidates": [
    {
      "num_iid": "123",
      "title": "红鸟黑色液体鞋油75g整箱批发",
      "price": 3.2,
      "sales": 1200,
      "detail_url": "https://detail.1688.com/offer/123.html",
      "sources": ["text", "image"],
      "detail": {},
      "seller_info": {}
    }
  ]
}
```

脚本输出 `final`、`rejected`、`rejected_reasons` 和每个候选的 `score_breakdown`。如果不能运行脚本，必须人工照 `references/final_filter_rules.md` 同样执行，不得改权重。

## API/MCP 结果归一化

无论候选来自 MCP、Onebound、浏览器还是手工表格，进入规则前都归一成这些字段:

- `num_iid` 或 `offerId`
- `title`
- `price`
- `sales`
- `pic_url`
- `detail_url`
- `sources`: `["text"]`、`["image"]` 或两者都有
- `detail`: 详情接口返回的 SKU、属性、库存、起批量、单位、图片
- `seller_info`: 店铺评分、经营年限、诚信通等

MCP/接口字段缺失时不要编造。缺 `seller_info` 时按中性分处理；缺 SKU 详情时按库存规则里的“待确认”模式处理。

## 输出要求

只写 1 个文件到任务输出目录:

- `找货_<商品简写>_<YYYYMMDD>.csv`: 给人看的 Top 5 表。

禁止用 `write_file` 写最终 JSON、`final.json`、`merged_input.json`、调试 JSON、Markdown 或文本报告到任务输出目录。需要中间 JSON 时，可以让脚本通过 `--output` 写临时文件，或用 `write_file` 写临时 JSON；系统会把这些临时文件放到隐藏 scratch 目录，不会作为产出上传。最终只调用一次 `write_file` 写 CSV。

CSV 要求:

- 首字符必须是 UTF-8 BOM。
- 数字列不写 `¥`，货币符号只放表头。
- 不写 Excel 公式。
- 详情链接必须是完整 URL。
- 推荐理由、拒绝/风险说明需要放在 CSV 列里，不另写 JSON 报告。
- 不足 5 条就标记“召回不足”，不改变原筛选权重凑数。

回复用户时给 Top 3:

```text
比价完成，找到 N 家合格供应商，Top 3:

1. <店铺/标题> <折算单价>，得分 <score>，<库存/起批数状态>
   https://detail.1688.com/offer/<id>.html
```

## 兜底浏览器注意事项

只有在 API/MCP 不可用时才使用浏览器。浏览器路线仍遵守这些约束:

- 取数据优先 JS 读页面数据，不用截图猜 DOM。
- 关键词必须来自 JD 标题或 SKU，禁止凭空换词。
- 图搜失败不算整单失败，降级到文搜候选。
- 遇验证码、滑块、登录失效，暂停并通知人，不写自动破解策略。
