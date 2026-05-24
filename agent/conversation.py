from __future__ import annotations

from collections import deque


class ConversationBuffer:
    """Per-chat ring buffer of last N (user_text, intent_label, reply_summary) tuples."""

    def __init__(self, max_per_chat: int = 5) -> None:
        self._max = max_per_chat
        self._buffers: dict[str, deque[tuple[str, str, str]]] = {}

    def append(self, chat_id: str, user_text: str, intent: str, summary: str) -> None:
        if chat_id not in self._buffers:
            self._buffers[chat_id] = deque(maxlen=self._max)
        self._buffers[chat_id].append((user_text, intent, summary))

    def recent(self, chat_id: str) -> list[tuple[str, str, str]]:
        return list(self._buffers.get(chat_id, []))
