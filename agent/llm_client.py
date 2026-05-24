from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._extra_body = extra_body or None

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._extra_body is not None:
            kwargs["extra_body"] = self._extra_body

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
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        return response.choices[0].message.content or ""
