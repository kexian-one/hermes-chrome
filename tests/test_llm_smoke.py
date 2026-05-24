from __future__ import annotations

import json

import pytest

from agent.config import load_config_file
from agent.llm_client import LLMClient


def _llm_settings_or_skip(role: str):
    cfg = load_config_file()
    if not cfg:
        pytest.skip(
            "no config.yaml found at project root. "
            "Copy config.example.yaml to config.yaml and fill in your keys."
        )
    try:
        llm = cfg["llm"][role]
        base_url, model, api_key = llm["base_url"], llm["model"], llm["api_key"]
    except KeyError as exc:
        pytest.skip(f"config.yaml missing llm.{role}.{exc.args[0]}")
    if not api_key or api_key.startswith("sk-REPLACE"):
        pytest.skip(f"llm.{role}.api_key is a placeholder; set a real key in config.yaml")
    return base_url, model, api_key


async def test_reasoning_chat_completion_basic() -> None:
    base_url, model, api_key = _llm_settings_or_skip("reasoning")
    llm = LLMClient(base_url=base_url, api_key=api_key, model=model)
    response = await llm.chat(
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
    )

    assert response.finish_reason in {"stop", "length"}
    assert response.text, "empty response text"
    print(f"\n  reasoning model={model}")
    print(f"  response: {response.text!r}")
    assert "PONG" in response.text.upper(), f"expected PONG marker, got: {response.text!r}"


async def test_reasoning_tool_calling_basic() -> None:
    base_url, model, api_key = _llm_settings_or_skip("reasoning")
    llm = LLMClient(base_url=base_url, api_key=api_key, model=model)

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

    print(f"\n  reasoning model={model}")
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
    base_url, model, api_key = _llm_settings_or_skip("multimodal")
    llm = LLMClient(base_url=base_url, api_key=api_key, model=model)
    response = await llm.chat(
        messages=[{"role": "user", "content": "Reply with exactly: VISION_OK"}],
    )

    assert response.text, "empty response text"
    print(f"\n  multimodal model={model}")
    print(f"  response: {response.text!r}")
    assert "VISION_OK" in response.text.upper(), (
        f"expected VISION_OK marker, got: {response.text!r}"
    )
