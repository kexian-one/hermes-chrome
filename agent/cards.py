"""飞书 interactive card helpers.

All bot replies render as cards for consistent layout — plain text wraps poorly
on mobile when content is structured (see Q11 / e2e screenshot).
"""

from __future__ import annotations


def info_card(title: str, body_markdown: str = "") -> dict:
    return _card(title, body_markdown, "blue")


def success_card(title: str, body_markdown: str = "") -> dict:
    return _card(title, body_markdown, "green")


def warning_card(title: str, body_markdown: str = "") -> dict:
    return _card(title, body_markdown, "orange")


def error_card(title: str, body_markdown: str = "") -> dict:
    return _card(title, body_markdown, "red")


def neutral_card(title: str, body_markdown: str = "") -> dict:
    return _card(title, body_markdown, "grey")


def code_card(title: str, code: str, lang: str = "") -> dict:
    fence = f"```{lang}\n{code}\n```"
    return _card(title, fence, "blue")


def _card(title: str, body_markdown: str, color: str) -> dict:
    elements: list[dict] = []
    if body_markdown.strip():
        elements.append({"tag": "markdown", "content": body_markdown})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }


def _action_element(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _button(label: str, value: dict, btn_type: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "value": value,
        "type": btn_type,
    }


def status_card_with_actions(title: str, body_markdown: str, worker_rows: list[str]) -> dict:
    """Worker status card with [重启浏览器] and [看日志] buttons per worker row.

    The 重启 button maps to `restart_browser` (NOT `restart_worker`) — that's
    what users actually mean when they see a worker in 断开连接 / 空闲 state
    and want to "fix it". `restart_worker` semantics (re-run last skill)
    were too implementation-internal and surfaced confusing "fapiao-1688
    mcp-failed" messages when the real issue was the browser bridge.
    """
    base = info_card(title, body_markdown)
    buttons = []
    for worker_id in worker_rows:
        buttons.append(_button(
            f"[{worker_id}] 重启浏览器",
            {"intent": "restart_browser", "args": {"worker_id": worker_id}},
            "danger",
        ))
        buttons.append(_button(
            f"[{worker_id}] 看日志",
            {"intent": "query_logs", "args": {"worker_id": worker_id}},
            "default",
        ))
    if buttons:
        base["elements"].append(_action_element(buttons))
    return base


def schedule_list_card_with_actions(title: str, body_markdown: str, entry_ids: list[int]) -> dict:
    """Schedule list card with [删除] button per entry."""
    base = info_card(title, body_markdown)
    buttons = [
        _button(
            f"[#{eid}] 删除",
            {"intent": "schedule_remove", "args": {"entry_id": eid}},
            "danger",
        )
        for eid in entry_ids
    ]
    if buttons:
        base["elements"].append(_action_element(buttons))
    return base


def freeform_card_with_stop(title: str, body_markdown: str, worker_id: str) -> dict:
    """Freeform dispatch card with [强制停止] button."""
    base = warning_card(title, body_markdown)
    stop_btn = _button(
        "强制停止",
        {"intent": "restart_worker", "args": {"worker_id": worker_id}},
        "danger",
    )
    base["elements"].append(_action_element([stop_btn]))
    return base
