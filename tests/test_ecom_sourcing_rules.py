from __future__ import annotations

import importlib.util
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

    by_id = {c["num_iid"]: c for c in result["final"]}
    assert by_id["low-service"]["score"] == 0
    assert by_id["low-service"]["recommendationLevel"] == "不推荐"
    assert by_id["no-invoice"]["score"] > 0
    assert by_id["no-invoice"]["rejection"] is None
    assert "invoiceSupport" not in by_id["no-invoice"]
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
  ai:
    matcher:
      base_url: https://llm.example/v1
      model: model-x
      api_key: ai-key-secret
      enabled: true
""",
        encoding="utf-8",
    )

    cfg = mod.load_ecom_config(tmp_path)
    status = cfg.masked_status()

    assert status["onebound"]["configured"] is True
    assert status["alphashop_mcp"]["configured"] is True
    assert status["ai"]["matcher"]["configured"] is True
    assert "secret" not in status["onebound"]["key"]
    assert "secret" not in status["alphashop_mcp"]["ak"]
    assert "secret" not in status["ai"]["matcher"]["api_key"]


def test_data_source_normalizes_and_merges_candidates() -> None:
    mod = _load_data_sources_module()

    text_item = mod.normalize_candidate({
        "offerId": "123",
        "originTitle": "<em>红鸟</em>黑色液体鞋油75g",
        "originImageUrl": "https://img.example/a.jpg",
        "detailUrl": "https://detail.1688.com/offer/123.html",
        "price": "3.20",
        "soldOut": "120",
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


def test_data_source_jwt_hs256_shape() -> None:
    mod = _load_data_sources_module()

    token = mod._jwt_hs256({"iss": "ak", "exp": 123}, "secret")

    assert token.count(".") == 2
    assert "=" not in token
