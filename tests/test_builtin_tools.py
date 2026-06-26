from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent.builtin_tools import (
    BROWSER_BUILTIN_TOOL_NAMES,
    BROWSER_BUILTIN_TOOLS,
    BUILTIN_TOOLS,
    BUILTIN_TOOL_NAMES,
    _extract_oicc_tab_id,
    _jd_b2b_detail_url,
    execute_builtin,
    is_builtin,
    is_browser_builtin,
)


def test_builtin_tools_schema_has_required_fields() -> None:
    for tool in BUILTIN_TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        assert "parameters" in fn


def test_is_builtin() -> None:
    assert is_builtin("write_file") is True
    assert is_builtin("run_ecom_script") is True
    assert is_builtin("javascript_tool") is False
    assert is_builtin("") is False
    assert is_browser_builtin("extract_jd_product_browser") is True
    assert is_browser_builtin("write_file") is False


def test_browser_builtin_tools_are_separate_from_non_browser_tools() -> None:
    assert "extract_jd_product_browser" in BROWSER_BUILTIN_TOOL_NAMES
    assert "extract_jd_product_browser" not in BUILTIN_TOOL_NAMES
    names = [tool["function"]["name"] for tool in BROWSER_BUILTIN_TOOLS]
    assert names == ["extract_jd_product_browser"]


def test_extract_oicc_tab_id_from_context_text() -> None:
    text = (
        '{"availableTabs":[{"tabId":21083066,"title":"about:blank"}],"tabGroupId":1}\n'
        'Tab Context: tabId 21083066'
    )
    assert _extract_oicc_tab_id(text) == 21083066


def test_jd_url_normalizes_to_b2b_detail_url() -> None:
    assert _jd_b2b_detail_url("https://item.jd.com/10177709350354.html") == (
        "https://b2b.jd.com/goods/goods-detail/10177709350354"
        "?sourceurl=/trade/goods-detail&bMallTag=1&buId=456"
    )
    assert _jd_b2b_detail_url(
        "https://b2b.jd.com/goods/goods-detail/10212040410264?sourceurl=/trade/goods-detail&bMallTag=1&buId=456"
    ) == (
        "https://b2b.jd.com/goods/goods-detail/10212040410264"
        "?sourceurl=/trade/goods-detail&bMallTag=1&buId=456"
    )


def test_write_file_writes_relative_path(tmp_path: Path) -> None:
    args = json.dumps({"path": "out/report.csv", "content": "a,b,c\n1,2,3\n"})
    result_json = execute_builtin("write_file", args, tmp_path)
    result = json.loads(result_json)
    assert result["ok"] is True
    assert result["path"].replace("\\", "/") == "out/report.csv"
    written = (tmp_path / "out" / "report.csv").read_text(encoding="utf-8")
    assert written == "a,b,c\n1,2,3\n"


def test_write_file_writes_bom_utf8_for_chinese(tmp_path: Path) -> None:
    chinese_csv = "﻿店铺,金额\n金华宅一族,2234.4\n"
    args = json.dumps({"path": "report_zh.csv", "content": chinese_csv})
    execute_builtin("write_file", args, tmp_path)
    raw = (tmp_path / "report_zh.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM should round-trip in bytes"


def test_write_file_refuses_path_outside_project(tmp_path: Path) -> None:
    args = json.dumps({"path": "../escape.txt", "content": "nope"})
    result_json = execute_builtin("write_file", args, tmp_path)
    result = json.loads(result_json)
    assert "error" in result
    assert "outside project root" in result["error"]


def test_write_file_refuses_absolute_path_outside_project(tmp_path: Path) -> None:
    outside = (tmp_path.parent / "outside.txt").resolve()
    args = json.dumps({"path": str(outside), "content": "nope"})
    result_json = execute_builtin("write_file", args, tmp_path)
    result = json.loads(result_json)
    assert "error" in result


def test_write_file_handles_invalid_json_args(tmp_path: Path) -> None:
    result_json = execute_builtin("write_file", "{not json", tmp_path)
    result = json.loads(result_json)
    assert "error" in result


def test_write_file_handles_missing_fields(tmp_path: Path) -> None:
    result_json = execute_builtin("write_file", "{}", tmp_path)
    result = json.loads(result_json)
    assert "error" in result


def test_unknown_builtin_returns_error(tmp_path: Path) -> None:
    result_json = execute_builtin("eat_pizza", "{}", tmp_path)
    result = json.loads(result_json)
    assert "error" in result
    assert "unknown builtin tool" in result["error"]


def test_run_ecom_script_keyword_builder() -> None:
    project_root = Path(__file__).parent.parent
    args = json.dumps({
        "script": "keyword_builder.py",
        "args": ["--title", "红鸟 RED BIRD 黑色液体鞋油 75g"],
        "timeout_seconds": 30,
    })
    result_json = execute_builtin("run_ecom_script", args, project_root)
    result = json.loads(result_json)
    assert result["ok"] is True
    assert result["script"] == "keyword_builder.py"
    assert "红鸟" in result["stdout"]


def test_run_ecom_script_refuses_unknown_script(tmp_path: Path) -> None:
    args = json.dumps({"script": "../agent/master.py"})
    result_json = execute_builtin("run_ecom_script", args, tmp_path)
    result = json.loads(result_json)
    assert "error" in result
    assert "not allowed" in result["error"]


def test_ecom_write_file_keeps_only_csv_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs" / "b1-task"
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))

    json_result = json.loads(execute_builtin(
        "write_file",
        json.dumps({"path": "final.json", "content": "{}"}),
        tmp_path,
    ))
    csv_result = json.loads(execute_builtin(
        "write_file",
        json.dumps({"path": "找货.csv", "content": "﻿排名,标题\n1,A\n"}),
        tmp_path,
    ))

    assert json_result["ok"] is True
    assert csv_result["ok"] is True
    assert (output_dir / ".ecom-scratch" / "final.json").is_file()
    assert (output_dir / "找货.csv").is_file()
    assert not (output_dir / "final.json").exists()


def test_ecom_run_script_writes_json_to_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).parent.parent
    output_dir = project_root / "outputs" / ".pytest-ecom-script-scratch"
    shutil.rmtree(output_dir, ignore_errors=True)
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(project_root))

    result_json = execute_builtin(
        "run_ecom_script",
        json.dumps({
            "script": "sourcing_rules.py",
            "args": ["--smoke", "--output", "smoke.json"],
            "timeout_seconds": 30,
        }),
        project_root,
    )
    result = json.loads(result_json)
    assert result["ok"] is True
    assert (output_dir / ".ecom-scratch" / "smoke.json").is_file()
    assert not (output_dir / "smoke.json").exists()
    shutil.rmtree(output_dir, ignore_errors=True)


def test_ecom_run_script_pipeline_writes_csv_visible_and_json_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).parent.parent
    output_dir = project_root / "outputs" / ".pytest-ecom-pipeline"
    scratch_dir = output_dir / ".ecom-scratch"
    shutil.rmtree(output_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True)
    monkeypatch.setenv("WORKER_SKILL_NAME", "ecom-best-source")
    monkeypatch.setenv("WORKER_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("WORKER_PROJECT_ROOT", str(project_root))

    (scratch_dir / "jd_product.json").write_text(
        json.dumps({
            "title": "红鸟 RED BIRD 黑色液体鞋油 75g",
            "jd_url": "https://item.jd.com/100012345678.html",
            "main_image_url": "https://img.example/jd.jpg",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (scratch_dir / "candidates.json").write_text(
        json.dumps({
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
                }
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    result_json = execute_builtin(
        "run_ecom_script",
        json.dumps({
            "script": "sourcing_pipeline.py",
            "args": [
                "--jd-product", "jd_product.json",
                "--candidates", "candidates.json",
                "--buy-multiple", "3",
                "--output", "找货_红鸟鞋油75g_20260615.csv",
                "--json-output", "final.json",
            ],
            "timeout_seconds": 30,
        }),
        project_root,
    )
    result = json.loads(result_json)
    assert result["ok"] is True
    assert (output_dir / "找货_红鸟鞋油75g_20260615.csv").is_file()
    assert (scratch_dir / "final.json").is_file()
    assert not (output_dir / "final.json").exists()
    assert (output_dir / "找货_红鸟鞋油75g_20260615.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    shutil.rmtree(output_dir, ignore_errors=True)
