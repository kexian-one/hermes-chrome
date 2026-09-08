# JD/B2B 浏览器商品信息采集

JD/B2B 商品信息的第一来源是当前 worker 连接的 OICC 浏览器页面。原因是京东价格、已选 SKU、起购倍数和部分品牌字段常由登录态、地区、动态接口或页面状态决定，静态 HTML 容易只拿到泛标题和图片。

## 执行顺序

1. 使用运行时注入的 MCP 浏览器工具，不手动指定端口、不连接其他 worker 的浏览器。
2. 从用户原始 URL 提取 `skuId`。无论用户给 `item.jd.com/<skuId>.html`，还是 `b2b.jd.com/goods/goods-detail/<skuId>`，实际打开都统一使用 `https://b2b.jd.com/goods/goods-detail/<skuId>?sourceurl=/trade/goods-detail&bMallTag=1&buId=456`。
3. 先调用 `tabs_context_mcp` 检查当前 MCP tab group；需要新页面时调用 `tabs_create_mcp` 创建临时 tab。没有 tab group 时可 `tabs_context_mcp(createIfEmpty=true)` 创建。
4. 调用 `navigate` 带临时 `tabId`，调用 `javascript_tool` 带 `action: javascript_exec` 和同一个 tabId。
5. 在当前页面一次提取 `title`、`image_urls`、`item_id`、`selected_sku`、`brand`、`price`、`jd_price`、`buy_multiple`。
6. 缺失字段用静态脚本补齐，不覆盖浏览器登录态字段。临时tab在finally中关闭。

## 页面读取

优先当前已选SKU的可见DOM，补充window.pageConfig.product和JSON-LD中ID与当前商品一致的对象。不遍历无关商品状态。当前商品图库主图优先，状态/OG图片补充。商品图片理解核对包装标签，不代替价格、库存、MOQ。

## 字段口径

- `item_id`: 从页面状态、URL `item.jd.com/<id>.html`、`sku`、`wareId` 或 `goods-detail/<id>` 提取。
- `image_urls`: 保留多张商品图并去重；`main_image_url` 是第一张最可信商品主图。
- `price` / `jd_price`: 页面展示的当前登录态价格。若页面只给区间或待询价，保留原始文本，不要编造数字。
- `selected_sku`: 页面当前选中的颜色、规格、包装、数量等 SKU 文本。
- `buy_multiple`: 京东万商或页面明确展示的起购倍数、整箱数量、最小采购数量；没有明确值时留空或 `1`，不要从 1688 起批量反推。

## 失败和回退

- 浏览器能打开但部分字段为空: 运行 `jd_product.py` 静态补 `title`、`item_id`、`main_image_url`、`image_urls`。
- MCP 连接失败、浏览器打不开、验证码、滑块、登录失效、整页风控或价格缺失: 不要卡住任务；运行 `jd_product.py` 静态补其他字段后继续召回和筛选。静态 HTML 不能提供可信登录态价格时，`jd_price` 留空，利润率留空或待确认。
- 静态脚本返回的泛标题、占位图或空字段不能覆盖浏览器已取得的登录态价格、SKU、品牌、起购倍数。
