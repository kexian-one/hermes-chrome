from __future__ import annotations

import json
import base64
import struct
import zlib

import pytest

from agent.config import load_config_file, _load_llm_pair
from agent.llm_client import LLMClient


def _llm_settings_or_skip(role: str):
    cfg = load_config_file()
    if not cfg:
        pytest.skip(
            "no config.yaml found at project root. "
            "Copy config.example.yaml to config.yaml and fill in your keys."
        )
    try:
        multimodal, reasoning = _load_llm_pair(cfg)
        settings = {"multimodal": multimodal, "reasoning": reasoning}[role]
    except KeyError as exc:
        pytest.skip(f"config.yaml missing llm.{role}.{exc.args[0]}")
    if not settings.api_key or settings.api_key.startswith("sk-REPLACE"):
        pytest.skip(f"llm.{role}.api_key is a placeholder; set a real key in config.yaml")
    return settings


def _client(settings) -> LLMClient:
    return LLMClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        provider=settings.provider,
        extra_body=settings.extra_body,
        max_tokens=settings.max_tokens,
        temperature=settings.temperature,
        reasoning_effort=settings.reasoning_effort,
        mcp_server_config=settings.mcp_server_config,
        mcp_tool=settings.mcp_tool,
    )


def _sample_png_b64() -> str:
    width, height = 240, 120
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((240, 60, 60) if 20 < x < 220 and 30 < y < 90 else (255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


async def test_reasoning_chat_completion_basic() -> None:
    settings = _llm_settings_or_skip("reasoning")
    llm = _client(settings)
    response = await llm.chat(
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
    )

    assert response.finish_reason in {"stop", "length"}
    assert response.text, "empty response text"
    print(f"\n  reasoning model={settings.model}")
    print(f"  response: {response.text!r}")
    assert "PONG" in response.text.upper(), f"expected PONG marker, got: {response.text!r}"


async def test_reasoning_tool_calling_basic() -> None:
    settings = _llm_settings_or_skip("reasoning")
    llm = _client(settings)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_current_weather",
                "description": "Get the current weather in a given city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                    },
                    "required": ["city"],
                },
            },
        }
    ]

    response = await llm.chat(
        messages=[{"role": "user", "content": "What is the weather in Beijing right now? Use the tool."}],
        tools=tools,
    )

    print(f"\n  reasoning model={settings.model}")
    print(f"  finish_reason={response.finish_reason}")
    print(f"  tool_calls={len(response.tool_calls)}")
    if response.tool_calls:
        tc = response.tool_calls[0]
        print(f"  tool={tc.name} args={tc.arguments!r}")

    assert response.tool_calls, (
        f"expected the model to issue a tool_call; got text only: {response.text!r}"
    )
    tc = response.tool_calls[0]
    assert tc.name == "get_current_weather"

    args = json.loads(tc.arguments)
    assert "city" in args
    assert "beijing" in args["city"].lower() or "北京" in args["city"]


async def test_multimodal_smoke() -> None:
    settings = _llm_settings_or_skip("multimodal")
    llm = _client(settings)
    if settings.provider == "zai_mcp":
        text = await llm.describe_image(_sample_png_b64(), "image/png")
        print(f"\n  multimodal provider={settings.provider} tool={settings.mcp_tool}")
        print(f"  response: {text[:300]!r}")
        assert text, "empty vision response text"
        assert "红" in text or "red" in text.lower()
        return

    response = await llm.chat(
        messages=[{"role": "user", "content": "Reply with exactly: VISION_OK"}],
    )
    assert response.text, "empty response text"
    print(f"\n  multimodal model={settings.model}")
    print(f"  response: {response.text!r}")
    assert "VISION_OK" in response.text.upper(), (
        f"expected VISION_OK marker, got: {response.text!r}"
    )
