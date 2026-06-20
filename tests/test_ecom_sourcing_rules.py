from __future__ import annotations

import importlib.util
import csv
import sys
from pathlib import Path


def _load_rules_module():
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
        / "sourcing_rules.py"
    )
    spec = importlib.util.spec_from_file_location("ecom_sourcing_rules", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_ecom_config_module():
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
        / "ecom_config.py"
    )
    spec = importlib.util.spec_from_file_location("ecom_config_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_data_sources_module():
    scripts_dir = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
    )
    sys.path.insert(0, str(scripts_dir))
    try:
        path = scripts_dir / "data_sources.py"
        spec = importlib.util.spec_from_file_location("ecom_data_sources", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts_dir))


def _load_pipeline_module():
    scripts_dir = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
    )
    sys.path.insert(0, str(scripts_dir))
    try:
        path = scripts_dir / "sourcing_pipeline.py"
        spec = importlib.util.spec_from_file_location("ecom_sourcing_pipeline", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts_dir))


def _load_keyword_module():
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
        / "keyword_builder.py"
    )
    spec = importlib.util.spec_from_file_location("ecom_keyword_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_original_skill_price_weight_makes_cheaper_candidate_win() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {"jd_price": 5.0, "buy_multiple": 10},
        "candidates": [
            {
                "num_iid": "expensive",
                "title": "A",
                "unitPrice": 4.0,
                "compositeScore": 5,
                "shopYear": 5,
                "repurchaseRate": "60%",
                "responseRate": "100%",
                "invoiceSupport": True,
                "invoiceType": "专票",
            },
            {
                "num_iid": "cheap",
                "title": "B",
                "unitPrice": 2.0,
                "compositeScore": 5,
                "shopYear": 5,
                "repurchaseRate": "60%",
                "responseRate": "100%",
                "invoiceSupport": True,
                "invoiceType": "专票",
            },
        ],
    })

    assert result["final"][0]["num_iid"] == "cheap"
    assert result["final"][0]["score_breakdown"]["price"] == 100
    assert round(result["weights"]["price"], 4) == 0.7143
    assert round(result["weights"]["composite_service"], 4) == 0.2857
    assert set(result["final"][0]["score_breakdown"]) == {"price", "composite_service"}
    assert "repurchaseRate" not in result["final"][0]
    assert "responseRate" not in result["final"][0]
    assert "invoiceSupport" not in result["final"][0]


def test_current_rule_downgrades_service_but_ignores_invoice_for_scoring() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {"jd_price": 5.0, "buy_multiple": 10},
        "candidates": [
            {
                "num_iid": "low-service",
                "title": "A",
                "unitPrice": 1.0,
                "compositeScore": 2.9,
                "shopYear": 5,
                "invoiceSupport": True,
                "invoiceType": "专票",
            },
            {
                "num_iid": "no-invoice",
                "title": "B",
                "unitPrice": 1.0,
                "compositeScore": 5,
                "shopYear": 5,
                "invoiceSupport": False,
            },
        ],
    })

    final_by_id = {c["num_iid"]: c for c in result["final"]}
    rejected_by_id = {c["num_iid"]: c for c in result["rejected"]}
    assert rejected_by_id["low-service"]["score"] == 0
    assert rejected_by_id["low-service"]["recommendationLevel"] == "不推荐"
    assert final_by_id["no-invoice"]["score"] > 0
    assert final_by_id["no-invoice"]["rejection"] is None
    assert "invoiceSupport" not in final_by_id["no-invoice"]
    assert result["rejected_reasons"]["综合服务分 < 3.0"] == 1
    assert "不能开发票" not in result["rejected_reasons"]


def test_original_skill_moq_high_is_warning_not_zero_score() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {"jd_price": 5.0, "buy_multiple": 10},
        "candidates": [
            {
                "num_iid": "moq-high",
                "title": "A",
                "unitPrice": 1.0,
                "compositeScore": 5,
                "shopYear": 5,
                "repurchaseRate": "60%",
                "responseRate": "100%",
                "invoiceSupport": True,
                "invoiceType": "专票",
                "MOQ": 101,
            },
        ],
    })

    item = result["final"][0]
    assert item["score"] > 0
    assert "MOQ 过高" in item["warnings"]


def test_seller_star_can_fill_original_composite_service_dimension() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {"jd_price": 5.0, "buy_multiple": 10},
        "candidates": [
            {
                "num_iid": "seller-star",
                "title": "A",
                "unitPrice": 1.0,
                "shopYear": 5,
                "seller_info": {"star": 4.5},
                "invoiceSupport": True,
                "invoiceType": "专票",
            },
        ],
    })

    assert result["final"][0]["score_breakdown"]["composite_service"] == 90


def test_spec_boundary_does_not_match_750g_for_75g() -> None:
    rules = _load_rules_module()

    assert rules.spec_in_text("75g", "红鸟液体鞋油75g") is True
    assert rules.spec_in_text("75g", "红鸟液体鞋油750g") is False
    assert rules.spec_in_text("650g", "清扬洗发水650ml") is True


def test_keyword_builder_does_not_emit_bare_english_alias_query() -> None:
    mod = _load_keyword_module()

    kw = mod.build_keywords(
        "清扬洗发水控油冰爽薄荷活力止痒韧发650克洗发水 男士深层净澈 650g 650ml",
        known_brand="清扬",
    )

    assert kw.to_query() == "清扬 洗发水 薄荷 650g"
    assert "CLEAR" not in kw.extra_queries()
    assert "CLEAR 洗发水 650g" in kw.extra_queries()


def test_relevance_filters_wrong_brand_category_and_sku() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {
            "title": "清扬洗发水控油冰爽薄荷活力止痒韧发650克洗发水 男士深层净澈 650g 650ml",
            "brand": "清扬",
            "brand_aliases": ["清扬", "CLEAR"],
            "category": "洗发水",
            "variant": ["薄荷"],
            "spec": "650g",
            "buy_multiple": 3,
        },
        "candidates": [
            {
                "num_iid": "bag",
                "title": "透明pvc自封袋首饰收纳袋塑料封口包装袋子",
                "unitPrice": 0.03,
                "compositeScore": 5,
                "shopYear": 5,
            },
            {
                "num_iid": "pantene",
                "title": "潘婷洗发水5g小包袋装洗发露",
                "unitPrice": 0.19,
                "compositeScore": 5,
                "shopYear": 5,
            },
            {
                "num_iid": "clear-100g",
                "title": "清扬男士去屑止痒洗发露活力运动薄荷型100克",
                "unitPrice": 3.6,
                "compositeScore": 5,
                "shopYear": 5,
            },
            {
                "num_iid": "clear-650",
                "title": "清扬洗发水控油冰爽薄荷活力发650克洗发水批发",
                "unitPrice": 37.5,
                "compositeScore": 4.8,
                "shopYear": 8,
                "detail": {
                    "min_num": 3,
                    "num": 1498,
                    "skus": {
                        "sku": [
                            {"properties_name": "净含量:650ml;规格类型:男士深层净澈", "quantity": 1498}
                        ]
                    },
                },
            },
        ],
    })

    assert [item["num_iid"] for item in result["final"]] == ["clear-650"]
    assert result["status"] == "召回不足"
    rejected = {item["num_iid"]: item["rejection"] for item in result["rejected"]}
    assert rejected["bag"] == "品牌不匹配"
    assert rejected["pantene"] == "品牌不匹配"
    assert rejected["clear-100g"] == "SKU不一致"


def test_obvious_no_stock_is_rejected_but_unknown_stock_can_remain() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {
            "title": "红鸟黑色液体鞋油75g",
            "brand": "红鸟",
            "category": "鞋油",
            "variant": ["黑色"],
            "spec": "75g",
        },
        "candidates": [
            {
                "num_iid": "zero-stock",
                "title": "红鸟黑色液体鞋油75g批发",
                "unitPrice": 2.8,
                "compositeScore": 5,
                "shopYear": 5,
                "detail": {
                    "skus": {
                        "sku": [
                            {"properties_name": "颜色:黑色;规格:75g", "quantity": 0}
                        ]
                    }
                },
            },
            {
                "num_iid": "unknown-stock",
                "title": "红鸟黑色液体鞋油75g批发",
                "unitPrice": 3.0,
                "compositeScore": 5,
                "shopYear": 5,
            },
        ],
    })

    assert [item["num_iid"] for item in result["final"]] == ["unknown-stock"]
    rejected = {item["num_iid"]: item["rejection"] for item in result["rejected"]}
    assert rejected["zero-stock"] in {"目标SKU无库存", "无库存"}


def test_unsuitable_price_and_low_score_are_removed_from_final() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {"jd_price": 10},
        "candidates": [
            {"num_iid": "cheap", "title": "A", "unitPrice": 2, "compositeScore": 5, "shopYear": 5},
            {"num_iid": "near-jd", "title": "B", "unitPrice": 10, "compositeScore": 5, "shopYear": 5},
            {"num_iid": "low-score", "title": "C", "unitPrice": 9, "compositeScore": 5, "shopYear": 5},
        ],
    })

    assert [item["num_iid"] for item in result["final"]] == ["cheap"]
    rejected = {item["num_iid"]: item["rejection"] for item in result["rejected"]}
    assert rejected["near-jd"] == "价格不低于京东"
    assert rejected["low-score"] == "综合得分 < 60"


def test_sourcing_rules_defaults_to_six_final_candidates() -> None:
    rules = _load_rules_module()
    result = rules.run_pipeline({
        "target": {},
        "candidates": [
            {"num_iid": str(i), "title": f"商品{i}", "unitPrice": 3, "compositeScore": 5, "shopYear": 5}
            for i in range(1, 8)
        ],
    })

    assert result["stats"]["target_count"] == 6
    assert result["stats"]["final_count"] == 6


def test_ecom_config_loads_private_section_without_exposing_values(tmp_path: Path) -> None:
    mod = _load_ecom_config_module()
    (tmp_path / "config.yaml").write_text(
        """
ecom_best_source:
  data_source: hybrid
  onebound:
    base: https://api.example/1688
    key: ob-key-secret
    secret: ob-secret-value
  alphashop_mcp:
    endpoint: https://mcp.example/sse
    ak: alpha-ak-secret
    sk: alpha-sk-secret
""",
        encoding="utf-8",
    )

    cfg = mod.load_ecom_config(tmp_path)
    status = cfg.masked_status()

    assert status["onebound"]["configured"] is True
    assert status["alphashop_mcp"]["configured"] is True
    assert "secret" not in status["onebound"]["key"]
    assert "secret" not in status["alphashop_mcp"]["ak"]
    assert "ai" not in status


def test_data_source_normalizes_and_merges_candidates() -> None:
    mod = _load_data_sources_module()

    text_item = mod.normalize_candidate({
        "offerId": "123",
        "originTitle": "<em>红鸟</em>黑色液体鞋油75g",
        "originImageUrl": "https://img.example/a.jpg",
        "detailUrl": "https://detail.1688.com/offer/123.html",
        "price": "3.20",
        "soldOut": "120",
        "sellerName": "红鸟源头店",
    }, source="text")
    image_item = mod.normalize_candidate({
        "num_iid": "123",
        "title": "红鸟黑色液体鞋油75g",
        "price": "3.10",
        "sales": 80,
    }, source="image")

    merged = mod.merge_candidates([text_item, image_item])

    assert len(merged) == 1
    assert merged[0]["num_iid"] == "123"
    assert merged[0]["title"] == "红鸟黑色液体鞋油75g"
    assert merged[0]["sources"] == ["image", "text"]
    assert merged[0]["shopName"] == "红鸟源头店"


def test_data_source_merges_onebound_detail_enrichment() -> None:
    mod = _load_data_sources_module()
    merged = mod._merge_detail_enrichment(
        {
            "num_iid": "123",
            "title": "MCP标题",
            "skus": {"sku": [{"quantity": 8}]},
            "seller_info": {"sid": "", "nick": ""},
        },
        {
            "num_iid": "123",
            "nick": "Onebound店铺",
            "seller_info": {"sid": "seller-123", "nick": "Onebound店铺", "star": "5.0", "tpyear": "6"},
        },
    )

    assert merged["title"] == "MCP标题"
    assert merged["skus"]["sku"][0]["quantity"] == 8
    assert merged["seller_info"]["sid"] == "seller-123"
    assert merged["seller_info"]["nick"] == "Onebound店铺"
    assert merged["seller_info"]["star"] == "5.0"
    assert merged["seller_info"]["tpyear"] == "6"


def test_sourcing_pipeline_shipping_text() -> None:
    mod = _load_pipeline_module()

    assert mod._shipping_text({"detail": {"freeDeliverFee": True}}) == "包邮"
    assert mod._shipping_text({"detail": {"post_fee": 4}}) == "首费4"
    assert mod._shipping_text({"detail": {"freightInfo": {"totalCost": 10.75}}}) == "10.75"
    assert mod._shipping_text({"detail": {}}) == "待确认"


def test_sourcing_pipeline_b9_summary_text_uses_first_link_and_all_shops() -> None:
    mod = _load_pipeline_module()

    text = mod._b9_summary_text({
        "final": [
            {"link": "https://detail.1688.com/offer/964004881912.html?source=kj_material_agent", "shopName": "天台县禹络百货店"},
            {"link": "https://detail.1688.com/offer/2.html", "shopName": "济南市历城区蓓盈贸易商行"},
            {"link": "https://detail.1688.com/offer/3.html", "shopName": "天台县禹络百货店"},
        ]
    })

    assert text == (
        "https://detail.1688.com/offer/964004881912.html?source=kj_material_agent"
        "，天台县禹络百货店，济南市历城区蓓盈贸易商行"
    )


def test_jd_product_parser_extracts_title_and_images() -> None:
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
        / "jd_product.py"
    )
    spec = importlib.util.spec_from_file_location("jd_product", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    html = """
    <html>
      <head>
        <title>红鸟 RED BIRD 黑色液体鞋油 75g - 京东</title>
        <meta property="og:title" content="红鸟 RED BIRD 黑色液体鞋油 75g">
        <meta property="og:image" content="//img14.360buyimg.com/n1/jfs/t1/abc.jpg!q70.webp">
      </head>
      <body>
        <img src="//img12.360buyimg.com/n1/jfs/t1/extra.jpg">
      </body>
    </html>
    """
    result = module.parse_product_html(html, "https://item.jd.com/100012345678.html")

    assert result["title"] == "红鸟 RED BIRD 黑色液体鞋油 75g"
    assert result["item_id"] == "100012345678"
    assert result["main_image_url"] == "https://img14.360buyimg.com/n1/jfs/t1/abc.jpg"
    assert "https://img12.360buyimg.com/n1/jfs/t1/extra.jpg" in result["image_urls"]


def test_jd_product_b2b_generic_falls_back_to_item_page(monkeypatch) -> None:
    path = (
        Path(__file__).parent.parent
        / "skills"
        / "ecom-best-source"
        / "scripts"
        / "jd_product.py"
    )
    spec = importlib.util.spec_from_file_location("jd_product_fallback", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    def fake_http(url: str, timeout: int) -> str:
        if "b2b.jd.com" in url:
            return """
            <html><head>
              <title>京东万商</title>
              <meta property="og:image" content="//img12.360buyimg.com/imagetools/shell.png">
            </head></html>
            """
        return """
        <html><head>
          <title>清扬洗发水控油冰爽薄荷650克,清扬（CLEAR）,,京东,网上购物</title>
          <meta property="og:image" content="//img11.360buyimg.com/imagetools/placeholder.png">
        </head>
        <body>
          <img src="//img10.360buyimg.com/n1/s720x720_jfs/t1/product.jpg">
        </body></html>
        """

    monkeypatch.setattr(module, "_http_get_text", fake_http)

    result = module.fetch_product("https://b2b.jd.com/goods/goods-detail/10117266111930", timeout=1)

    assert result["title"] == "清扬洗发水控油冰爽薄荷650克"
    assert result["item_id"] == "10117266111930"
    assert result["main_image_url"] == "https://img10.360buyimg.com/n1/s720x720_jfs/t1/product.jpg"


def test_data_source_jwt_hs256_shape() -> None:
    mod = _load_data_sources_module()

    token = mod._jwt_hs256({"iss": "ak", "exp": 123}, "secret")

    assert token.count(".") == 2
    assert "=" not in token


def test_sourcing_pipeline_writes_final_csv_with_bom(tmp_path: Path) -> None:
    mod = _load_pipeline_module()
    jd_product = tmp_path / "jd_product.json"
    candidates = tmp_path / "candidates.json"
    output = tmp_path / "找货_红鸟鞋油75g_20260615.csv"

    jd_product.write_text(
        """
{
  "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
  "jd_url": "https://item.jd.com/100012345678.html",
  "item_id": "100012345678",
  "main_image_url": "https://img.example/jd.jpg"
}
""",
        encoding="utf-8",
    )
    candidates.write_text(
        """
{
  "candidates": [
    {
      "num_iid": "1001",
      "title": "红鸟黑色液体鞋油75g批发",
      "detail_url": "https://detail.1688.com/offer/1001.html",
      "unitPrice": 3.2,
      "compositeScore": 4.8,
      "shopYear": 8,
      "MOQ": 3,
      "shopName": "义乌红鸟日化",
      "sources": ["text", "image"],
      "detail": {
        "num": 30,
        "skus": {
          "sku": [
            {"properties_name": "颜色:黑色;规格:75g", "quantity": 12, "price": 3.2}
          ]
        }
      }
    }
  ]
}
""",
        encoding="utf-8",
    )

    summary = mod.run_from_files(
        jd_product_path=str(jd_product),
        candidates_path=str(candidates),
        merged_input_path=None,
        output_path=str(output),
        buy_multiple=3,
        target_count=5,
    )

    assert summary["final_count"] == 1
    assert summary["top3"][0]["link"] == "https://detail.1688.com/offer/1001.html"
    assert summary["top3"][0]["stock"] == "匹配SKU库存 12"
    raw = output.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = output.read_text(encoding="utf-8-sig")
    headers = text.splitlines()[0].split(",")
    assert headers == [
        "1688商品标题",
        "价格(元)",
        "邮费",
        "起批数",
        "规格匹配",
        "SKU库存",
        "店铺信息",
        "综合服务分",
        "经营年限",
        "风险说明",
        "1688链接",
    ]
    assert "排名" not in headers
    assert "得分" not in headers
    assert "推荐理由" not in headers
    assert "红鸟黑色液体鞋油75g批发" in text
    assert "https://detail.1688.com/offer/1001.html" in text
    assert "SKU库存" in text
    rows = list(csv.reader(output.open(encoding="utf-8-sig")))
    assert rows[8][1] == "https://detail.1688.com/offer/1001.html，义乌红鸟日化"


def test_sourcing_pipeline_confirms_final_details_and_removes_no_stock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mod = _load_pipeline_module()
    jd_product = tmp_path / "jd_product.json"
    candidates = tmp_path / "candidates.json"
    output = tmp_path / "找货_红鸟鞋油75g_20260617.csv"

    jd_product.write_text(
        """
{
  "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
  "jd_url": "https://item.jd.com/100012345678.html",
  "item_id": "100012345678",
  "main_image_url": "https://img.example/jd.jpg"
}
""",
        encoding="utf-8",
    )
    candidates.write_text(
        """
{
  "candidates": [
    {
      "num_iid": "1001",
      "title": "红鸟黑色液体鞋油75g批发",
      "detail_url": "https://detail.1688.com/offer/1001.html",
      "unitPrice": 2.9,
      "compositeScore": 5,
      "shopYear": 5,
      "MOQ": 3,
      "shopName": "无货店"
    },
    {
      "num_iid": "1002",
      "title": "红鸟黑色液体鞋油75g批发",
      "detail_url": "https://detail.1688.com/offer/1002.html",
      "unitPrice": 3.2,
      "compositeScore": 5,
      "MOQ": 3
    }
  ]
}
""",
        encoding="utf-8",
    )

    calls: list[str] = []

    class FakeClient:
        def item_get(self, num_iid: str) -> dict[str, object]:
            calls.append(num_iid)
            if num_iid == "1001":
                return {
                    "price": 2.9,
                    "num": 0,
                    "skus": {"sku": [{"properties_name": "颜色:黑色;规格:75g", "quantity": 0}]},
                }
            return {
                "price": 3.2,
                "num": 18,
                "seller_info": {"sid": "seller-1002", "nick": "详情店"},
                "skus": {"sku": [{"properties_name": "颜色:黑色;规格:75g", "quantity": 18}]},
            }

        def seller_info(self, sid: str) -> dict[str, object]:
            assert sid == "seller-1002"
            return {"sid": sid, "nick": "详情店", "star": "5.0", "tpyear": "6"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "_make_data_client", lambda: FakeClient())

    summary = mod.run_from_files(
        jd_product_path=str(jd_product),
        candidates_path=str(candidates),
        merged_input_path=None,
        output_path=str(output),
        known_brand="红鸟",
        buy_multiple=3,
        target_count=1,
    )

    assert calls == ["1001", "1002"]
    assert summary["confirmation"]["confirmed"] == 2
    assert summary["final_count"] == 1
    assert summary["top3"][0]["link"] == "https://detail.1688.com/offer/1002.html"
    assert summary["top3"][0]["shop"] == "详情店"
    assert summary["top3"][0]["stock"] == "匹配SKU库存 18"
    text = output.read_text(encoding="utf-8-sig")
    assert "详情店" in text
    assert ",5,6," in text
    assert "无货店" not in text


def test_sourcing_pipeline_reconfirms_existing_sku_detail_when_seller_fields_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mod = _load_pipeline_module()
    jd_product = tmp_path / "jd_product.json"
    candidates = tmp_path / "candidates.json"
    output = tmp_path / "找货_红鸟鞋油75g_20260620.csv"

    jd_product.write_text(
        """
{
  "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
  "jd_url": "https://item.jd.com/100012345678.html",
  "item_id": "100012345678",
  "main_image_url": "https://img.example/jd.jpg"
}
""",
        encoding="utf-8",
    )
    candidates.write_text(
        """
{
  "candidates": [
    {
      "num_iid": "1006",
      "title": "红鸟黑色液体鞋油75g批发",
      "detail_url": "https://detail.1688.com/offer/1006.html",
      "unitPrice": 3.2,
      "MOQ": 3,
      "detail": {
        "num": 18,
        "skus": {"sku": [{"properties_name": "颜色:黑色;规格:75g", "quantity": 18}]}
      }
    }
  ]
}
""",
        encoding="utf-8",
    )

    calls: list[str] = []

    class FakeClient:
        def item_get(self, num_iid: str) -> dict[str, object]:
            calls.append(num_iid)
            return {
                "price": 3.2,
                "num": 18,
                "seller_info": {"sid": "seller-1006", "nick": "第六店"},
                "skus": {"sku": [{"properties_name": "颜色:黑色;规格:75g", "quantity": 18}]},
            }

        def seller_info(self, sid: str) -> dict[str, object]:
            assert sid == "seller-1006"
            return {"sid": sid, "nick": "第六店", "star": "4.9", "tpyear": "7"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "_make_data_client", lambda: FakeClient())

    summary = mod.run_from_files(
        jd_product_path=str(jd_product),
        candidates_path=str(candidates),
        merged_input_path=None,
        output_path=str(output),
        known_brand="红鸟",
        buy_multiple=3,
        target_count=6,
    )

    assert calls == ["1006"]
    assert summary["confirmation"]["confirmed"] == 1
    text = output.read_text(encoding="utf-8-sig")
    assert "第六店" in text
    assert ",4.9,7," in text
