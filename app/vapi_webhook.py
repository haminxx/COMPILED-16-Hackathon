"""Vapi server events. Tool handlers read cached session state only."""

from __future__ import annotations

import json
import logging
import time

from . import config, monitor, replay
from .briefing import ceiling_text, situation_text, traffic_text, weather_text
from .state import STORE, Session, normalize_phone
from .traffic import confirm_rules, merge_rules

log = logging.getLogger("freq.webhook")


def authorized(token: str) -> bool:
    return token == config.WEBHOOK_TOKEN and bool(token)


async def handle(payload: dict) -> dict:
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        message = payload if isinstance(payload, dict) else {}
    kind = str(message.get("type") or "").lower()
    log.info("webhook %s", kind or "unknown")
    if kind == "status-update":
        _on_status(message)
        return {"ok": True}
    if kind == "tool-calls":
        return {"results": _on_tools(message)}
    if kind in {"end-of-call-report", "end-of-call"}:
        call = message.get("call") or {}
        session = STORE.end_call(call)
        if session:
            replay.stop(session)
        return {"ok": True}
    return {"ok": True}


def _on_status(message: dict) -> None:
    call = message.get("call") or {}
    status = str(message.get("status") or call.get("status") or "").lower()
    if status in {"ended", "ending"}:
        session = STORE.end_call(call)
        if session:
            replay.stop(session)
        return
    if status not in {"in-progress", "ringing", "forwarding"}:
        return
    session = STORE.ensure_call(call)
    control = ((call.get("monitor") or {}).get("controlUrl"))
    STORE.bind_call(session, call.get("id"), control)
    _apply_demo_location(session)
    if status == "in-progress":
        if config.DEMO_MODE == "replay" and session.replay_t0 is None:
            replay.start(session)
        session.log("call", "Call connected")
        monitor.ensure(session)


def _on_tools(message: dict) -> list[dict]:
    call = message.get("call") or {}
    session = STORE.ensure_call(call)
    _apply_demo_location(session)
    results = []
    for tool_id, name, args in _extract_calls(message):
        try:
            text = _dispatch(session, call, name, args)
        except Exception:
            log.exception("tool %s failed", name)
            text = "I couldn't read that just now. Ask me again."
        results.append({"toolCallId": tool_id, "result": text})
        session.log("tool", f"{name}: {text[:180]}")
    return results


def _dispatch(session: Session, call: dict, name: str, args: dict) -> str:
    key = (name or "").strip()
    if key == "get_situation":
        return situation_text(session)
    if key == "get_ceiling":
        return ceiling_text(session)
    if key == "get_nearby_traffic":
        radius = args.get("radius_nm")
        return traffic_text(session, float(radius) if radius not in (None, "") else None)
    if key == "get_weather":
        return weather_text(session)
    if key == "set_rule":
        session.rules = merge_rules(session.rules, args)
        return confirm_rules(session.rules)
    if key == "link_session":
        return _link(session, call, str(args.get("code") or ""))
    return "That tool is not available."


def _link(session: Session, call: dict, code: str) -> str:
    target = STORE.for_code(code)
    if target is None:
        return "I don't recognize that Freq code. It is the 4-digit number on the start page."
    control = session.control_url or ((call.get("monitor") or {}).get("controlUrl"))
    call_id = call.get("id") or session.call_id
    if session is not target:
        session.active = False
        task = session.monitor
        session.monitor = None
        if task is not None and not task.done():
            task.cancel()
    STORE.bind_call(target, call_id, control)
    _apply_demo_location(target)
    if config.DEMO_MODE == "replay" and target.replay_t0 is None:
        replay.start(target)
    monitor.ensure(target)
    return "Linked. " + situation_text(target)


def _apply_demo_location(session: Session) -> None:
    if session.lat is not None or not config.DEMO_LOCATION:
        return
    parts = [part.strip() for part in config.DEMO_LOCATION.split(",")]
    if len(parts) != 2:
        return
    try:
        session.lat = float(parts[0])
        session.lon = float(parts[1])
    except ValueError:
        return
    session.loc_source = "simulated"
    session.loc_ts = session.loc_ts or time.time()
    session.log("location", "Using the pinned demo location")


def _extract_calls(message: dict) -> list[tuple[str, str, dict]]:
    raw = message.get("toolCallList") or message.get("toolWithToolCallList") or []
    extracted = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else None
        if function:
            name = function.get("name") or item.get("name") or ""
            args = function.get("arguments")
        else:
            tool = item.get("toolCall") if isinstance(item.get("toolCall"), dict) else item
            function = tool.get("function") if isinstance(tool.get("function"), dict) else None
            name = (function or {}).get("name") or tool.get("name") or item.get("name") or ""
            args = (function or {}).get("arguments") if function else tool.get("arguments")
            item = tool if tool is not item else item
        tool_id = item.get("id") or item.get("toolCallId") or ""
        extracted.append((str(tool_id), str(name), _args(args)))
    return extracted


def _args(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def customer_number(call: dict | None) -> str:
    return normalize_phone(((call or {}).get("customer") or {}).get("number"))
