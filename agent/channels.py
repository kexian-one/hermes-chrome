"""Channel-agnostic reply target.

The master and intent layers must NOT depend on any specific bot SDK
(Feishu, DingTalk, WeCom, Slack, etc.). All they need is:

    1. Send a card reply.
    2. Optionally, send a file artifact (if the channel supports it).

A `ReplyTarget` wraps that contract. Each concrete channel implementation
(``agent.bot`` for Feishu, future modules for others) creates ReplyTargets
when it receives a message and passes them through to the dispatcher.

When the user @-mentions a bot in chat A on Feishu and the bot spawns a
worker, the worker's completion notification routes back to chat A on the
same Feishu instance — not to a globally-configured "alert chat" — because
the ReplyTarget carries the origin coordinates.

Cron-fired tasks (no human origin) use ``MasterDispatcher.default_reply_target()``,
which the master picks from the first registered channel.

Duck-typed channel contract:
    async def send(self, target_id: str, payload: dict) -> None
        # required. payload is channel-specific (Feishu uses {"card": {...}})
    async def send_file(self, target_id: str, file_path: str) -> None
        # optional. ReplyTarget.send_file checks hasattr before calling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReplyTarget:
    """Where to send a reply or notification.

    `channel`        — channel handle (duck-typed; see module docstring)
    `target_id`      — channel-specific recipient (chat_id, conversation_id, ...)
    `supports_files` — whether the channel can upload binary artifacts
    `machine_name`   — label of the bot this target belongs to. Used in card
                       titles so multi-bot setups show which bot a notification
                       came from (e.g. `[pc-jianghu-finance] b2 任务完成`).
    """
    channel: Any
    target_id: str
    supports_files: bool = False
    machine_name: str = ""

    @property
    def is_valid(self) -> bool:
        return self.channel is not None and bool(self.target_id)

    async def send_card(self, card: dict) -> None:
        """Best-effort: send a card. Failures are logged but do not raise."""
        if not self.is_valid:
            return
        try:
            await self.channel.send(self.target_id, {"card": card})
        except Exception as exc:
            print(f"[reply-target] send_card failed: {exc}", flush=True)

    async def send_file(self, file_path: Path) -> bool:
        """Send a file if the channel supports it.

        Returns True if the upload was attempted and succeeded.
        Returns False if the channel doesn't support files, no file,
        or the upload failed.
        """
        if not self.is_valid or not self.supports_files:
            return False
        if not hasattr(self.channel, "send_file"):
            return False
        if not file_path.is_file():
            return False
        try:
            await self.channel.send_file(self.target_id, str(file_path))
            return True
        except Exception as exc:
            print(f"[reply-target] send_file({file_path.name}) failed: {exc}", flush=True)
            return False
