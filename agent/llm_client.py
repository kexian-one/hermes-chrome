from __future__ import annotations

import base64
import os
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResponse:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    reasoning_content: str | None = None


class LLMClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        extra_body: dict | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        provider: str = "openai",
        mcp_server_config: dict[str, Any] | None = None,
        mcp_tool: str | None = None,
    ) -> None:
        """`extra_body` is forwarded on every chat() — used for non-standard
        OpenAI-compatible knobs:
        - DeepSeek thinking control: `{"thinking": {"type": "disabled"}}`
          (DeepSeek defaults to thinking enabled, which generates a multi-
          thousand-token reasoning chain on every call — fine for hard
          reasoning, terrible for quick intent classification.)
        - Anthropic-style cache_control if proxied through compatible adapter
        - any other vendor-specific extras
        """
        self._provider = provider
        self._api_key = api_key
        self._client = None if provider == "zai_mcp" else AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._extra_body = extra_body or None
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._mcp_server_config = mcp_server_config or None
        self._mcp_tool = mcp_tool or "analyze_image"

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        if self._client is None:
            raise RuntimeError(f"chat() is unavailable for provider={self._provider}")
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._extra_body is not None:
            kwargs["extra_body"] = self._extra_body
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._reasoning_effort

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ))

        return ChatResponse(
            text=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            reasoning_content=getattr(msg, "reasoning_content", None),
        )

    async def describe_image(self, base64_data: str, mime_type: str = "image/png") -> str:
        """Call this client's (multimodal) model to produce a text description
        of a screenshot. Used by worker.py when a tool result contains image
        blocks — the description replaces the image so the downstream text-only
        reasoning model can act on it.

        Prompt asks for UI-actionable observations (buttons, inputs, popups,
        coordinates) rather than free-form scene description.
        """
        if self._provider == "zai_mcp":
            return await self._describe_image_with_zai_mcp(base64_data, mime_type, prompt=None)
        prompt = (
            "这是一张浏览器截图。用中文给出**当前页面的可操作 UI 状态**,目的是让另一个"
            "纯文本 LLM 据此决定下一步该点哪 / 输什么。重点列出:\n"
            "- 顶部导航 / 标题区(谁在哪个页面)\n"
            "- **所有可见输入框**(placeholder 文字 + 是否已有内容 + 大致中心坐标 [x, y])\n"
            "- **所有按钮**(按钮文字 + 颜色 + 中心坐标 [x, y])\n"
            "- 任何弹窗 / 错误提示 / 滑块验证 / 登录提示\n"
            "- 关键业务文本(订单号 / 商家名 / 状态 等)\n"
            "坐标用屏幕像素,左上为原点。每行一个观察,保持简洁。"
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_data}"}},
            ],
        }]
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        assert self._client is not None
        response = await self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    async def _describe_image_with_zai_mcp(
        self,
        base64_data: str,
        mime_type: str,
        prompt: str | None,
    ) -> str:
        if not self._mcp_server_config:
            raise RuntimeError("zai_mcp provider requires mcp_server_config")
        prompt = prompt or (
            "这是一张浏览器截图。请用中文描述当前页面的可操作 UI 状态，重点列出可见输入框、"
            "按钮、弹窗、错误提示、登录/验证码/滑块提示和关键业务文本。坐标如可判断也请给出。"
        )
        suffix = ".png"
        if "jpeg" in mime_type or "jpg" in mime_type:
            suffix = ".jpg"
        elif "webp" in mime_type:
            suffix = ".webp"
        image_bytes = base64.b64decode(base64_data)
        with tempfile.NamedTemporaryFile(prefix="zai-vision-", suffix=suffix, delete=False) as tmp:
            tmp.write(image_bytes)
            image_path = tmp.name
        try:
            return await self._call_zai_mcp_tool(image_path, prompt)
        finally:
            try:
                Path(image_path).unlink(missing_ok=True)
            except OSError:
                pass

    async def _call_zai_mcp_tool(self, image_path: str, prompt: str) -> str:
        cfg = self._mcp_server_config or {}
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in dict(cfg.get("env") or {}).items()})
        env.setdefault("Z_AI_API_KEY", self._api_key)
        params = StdioServerParameters(
            command=str(cfg.get("command") or "npx"),
            args=[str(x) for x in (cfg.get("args") or ["-y", "@z_ai/mcp-server"])],
            env=env,
            cwd=str(Path.cwd()),
        )
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.call_tool(
                self._mcp_tool,
                {"image_source": image_path, "prompt": prompt},
            )
            parts: list[str] = []
            for item in result.content or []:
                if hasattr(item, "text"):
                    parts.append(str(item.text))
                elif hasattr(item, "model_dump"):
                    data = item.model_dump()
                    if isinstance(data, dict):
                        parts.append(str(data.get("text") or data))
                else:
                    parts.append(str(item))
            if result.isError:
                raise RuntimeError("\n".join(parts) or "zai mcp image analysis failed")
            return "\n".join(p for p in parts if p).strip()
