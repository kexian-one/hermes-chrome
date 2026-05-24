"""Probe what NLU actually sends to DeepSeek + what it returns.

Reproduces the exact prompt + params the bot would send for the "重试一下"
case (parent_id reply scenario after the fetch_message fix).

Usage:
    python -m scripts.nlu_probe
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.config import load_config_file, _llm_from_dict
from agent.llm_client import LLMClient
from agent.nlu import _build_system_prompt, _format_recent_context


async def main() -> None:
    cfg = load_config_file()
    reasoning = _llm_from_dict(cfg["llm"]["reasoning"], "reasoning")
    skills_dir = Path(cfg["skills"]["dir"]).resolve()

    # Same client config as bot.py (thinking disabled for NLU)
    llm = LLMClient(
        base_url=reasoning.base_url,
        api_key=reasoning.api_key,
        model=reasoning.model,
        extra_body={"thinking": {"type": "disabled"}},
    )

    # Simulate the EXACT scenario user just tested:
    # - User had a prior message in chat: "@ai助手 b2 获取申请中发票"
    # - Then "重试" — pulled from Feishu group history (no intent labels)
    recent = [
        ("@ai助手 b2 获取申请中发票", "", ""),
        ("查状态", "", ""),
    ]
    # Reply quote case: user explicitly quoted the prior task message
    user_text = "[用户引用了上一条消息]: b2 获取申请中发票\n[用户本次说]: @ai助手 重试一下"

    context_block = _format_recent_context(recent)
    user_content = f"Recent conversation:\n{context_block}\n\nCurrent message: {user_text}"

    system_prompt = _build_system_prompt(skills_dir)

    print("=" * 80)
    print("SYSTEM PROMPT (truncated to last 1500 chars for readability):")
    print("=" * 80)
    print(system_prompt[-1500:])
    print()
    print("=" * 80)
    print("USER MESSAGE:")
    print("=" * 80)
    print(user_content)
    print()
    print("=" * 80)
    print("REQUEST PARAMS:")
    print("=" * 80)
    print(f"  base_url: {reasoning.base_url}")
    print(f"  model:    {reasoning.model}")
    print(f"  extra_body: {{'thinking': {{'type': 'disabled'}}}}")
    print()
    print("=" * 80)
    print("CALLING DEEPSEEK...")
    print("=" * 80)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = await llm.chat(messages)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return

    print(f"finish_reason:    {resp.finish_reason}")
    print(f"reasoning_content: {resp.reasoning_content!r}")
    print(f"text:             {resp.text!r}")
    print(f"tool_calls:       {resp.tool_calls}")

    # Parse the JSON to see what intent
    if resp.text:
        try:
            data = json.loads(resp.text.strip())
            print(f"\nPARSED → intent={data.get('intent')!r}, args={data.get('args')!r}")
        except json.JSONDecodeError as e:
            print(f"\nJSON PARSE FAILED: {e}")
            print(f"raw: {resp.text!r}")


if __name__ == "__main__":
    asyncio.run(main())
