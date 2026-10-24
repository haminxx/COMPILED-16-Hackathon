"""Vapi live-call control. Field names match the verified say / add-message schema."""

from __future__ import annotations

import logging
import time

from .sources import post_json

log = logging.getLogger("freq.control")

_last_injection = 0.0
GAP_S = 8.0


def reset_throttle() -> None:
    global _last_injection
    _last_injection = 0.0


async def say(control_url: str, content: str, *, interrupt: bool) -> bool:
    payload = {
        "type": "say",
        "content": content,
        "endCallAfterSpoken": False,
        "interruptAssistantEnabled": bool(interrupt),
        "interruptionsEnabled": True,
    }
    return await _post(control_url, payload)


async def add_message(control_url: str, content: str) -> bool:
    payload = {
        "type": "add-message",
        "message": {"role": "system", "content": content},
        "triggerResponseEnabled": True,
    }
    return await _post(control_url, payload)


async def dispatch(control_url: str, kind: str, content: str, *, interrupt: bool = False) -> bool:
    if kind == "add":
        return await add_message(control_url, content)
    return await say(control_url, content, interrupt=interrupt)


async def _post(control_url: str, payload: dict) -> bool:
    global _last_injection
    if not control_url:
        return False
    now = time.monotonic()
    if _last_injection and now - _last_injection < GAP_S:
        log.info("injection throttled (%ss)", GAP_S)
        return False
    result = await post_json(control_url, payload, timeout=4.0)
    if result is None:
        return False
    status, body = result
    if status >= 400:
        log.warning("control %s: %s", status, body[:240])
        return False
    _last_injection = time.monotonic()
    log.info("injected %s (%s chars)", payload.get("type"), len(payload.get("content") or payload.get("message", {}).get("content", "")))
    return True
