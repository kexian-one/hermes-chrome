from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.builtin_tools import (
    BUILTIN_TOOLS,
    BUILTIN_TOOL_NAMES,
    execute_builtin,
    is_builtin,
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
    assert is_builtin("javascript_tool") is False
    assert is_builtin("") is False


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
