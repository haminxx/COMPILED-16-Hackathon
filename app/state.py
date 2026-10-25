"""In-memory flight sessions. Keyed by the last 10 digits of the phone number."""

from __future__ import annotations

import itertools
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _blank_rules() -> dict[str, Any]:
    return {
        "radius_nm": None,
        "max_alt_ft": None,
        "include_types": [],
        "exclude_types": [],
        "quiet": False,
    }


def _blank_memory() -> dict[str, Any]:
    return {"cooldowns": {}, "ranges": {}, "last_injection": None}


@dataclass
class Session:
    id: str
    phone: str
    code: str
    call_id: str | None = None
    control_url: str | None = None
    active: bool = False
    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    loc_ts: float | None = None
    loc_source: str = "phone"
    loc_pinned: bool = False
    ceiling: dict | None = None
    ceiling_anchor: tuple[float, float] | None = None
    traffic: list[dict] = field(default_factory=list)
    weather: dict | None = None
    weather_ts: float = 0.0
    ground_ft: float | None = None
    rules: dict = field(default_factory=_blank_rules)
    memory: dict = field(default_factory=_blank_memory)
    events: deque = field(default_factory=lambda: deque(maxlen=80))
    replay_t0: float | None = None
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    call_started: float | None = None
    last_all_clear: float | None = None
    lines: list = field(default_factory=list)
    partial: dict | None = None
    monitor: Any = None

    def touch(self) -> None:
        self.touched = time.time()

    def log(self, kind: str, text: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        self.touch()

    @property
    def position_age_s(self) -> float | None:
        if self.loc_ts is None:
            return None
        return max(0.0, time.time() - self.loc_ts)

    @property
    def replay_active(self) -> bool:
        return self.replay_t0 is not None


class Store:
    def __init__(self) -> None:
        self.by_phone: dict[str, Session] = {}
        self.by_code: dict[str, Session] = {}
        self.by_call: dict[str, Session] = {}
        self.by_id: dict[str, Session] = {}
        self._codes = set(itertools.islice(
            (f"{n:04d}" for n in random.sample(range(10000), 10000)),
            10000,
        ))

    def _mint_code(self) -> str:
        if not self._codes:
            return f"{random.randint(0, 9999):04d}"
        code = self._codes.pop()
        while code in self.by_code:
            if not self._codes:
                return f"{random.randint(0, 9999):04d}"
            code = self._codes.pop()
        return code

    def _add(self, session: Session) -> Session:
        self.by_id[session.id] = session
        if session.phone:
            self.by_phone[session.phone] = session
        if session.code:
            self.by_code[session.code] = session
        return session

    def ensure_phone(self, phone: str) -> Session:
        key = normalize_phone(phone)
        existing = self.by_phone.get(key)
        if existing:
            existing.touch()
            return existing
        session = Session(id=uuid.uuid4().hex[:12], phone=key, code=self._mint_code())
        return self._add(session)

    def ensure_call(self, call: dict | None) -> Session:
        call = call or {}
        call_id = call.get("id")
        number = ((call.get("customer") or {}).get("number")) or ""
        key = normalize_phone(number)
        phone_session = self.by_phone.get(key) if key else None
        if call_id and call_id in self.by_call:
            session = self.by_call[call_id]
            if (
                phone_session is not None
                and phone_session is not session
                and phone_session.lat is not None
                and session.lat is None
            ):
                if session.lines and not phone_session.lines:
                    phone_session.lines = list(session.lines)
                    phone_session.partial = session.partial
                self.bind_call(phone_session, call_id, session.control_url)
                session.active = False
                session = phone_session
            session.touch()
            return session
        if phone_session is not None:
            session = phone_session
        else:
            session = Session(id=uuid.uuid4().hex[:12], phone=key, code="")
            self._add(session)
        self.bind_call(session, call_id, _control_url(call))
        return session

    def bind_call(self, session: Session, call_id: str | None, control_url: str | None) -> None:
        if call_id:
            previous = self.by_call.get(call_id)
            if previous is not None and previous is not session:
                previous.active = False
            session.call_id = call_id
            self.by_call[call_id] = session
        if control_url:
            session.control_url = control_url
        if call_id or control_url:
            session.active = True
            if session.call_started is None:
                session.call_started = time.time()
        session.touch()

    def end_call(self, call: dict | None) -> Session | None:
        call = call or {}
        session = None
        call_id = call.get("id")
        if call_id and call_id in self.by_call:
            session = self.by_call[call_id]
        if session is None:
            key = normalize_phone(((call.get("customer") or {}).get("number")) or "")
            session = self.by_phone.get(key)
        if session is None:
            return None
        session.active = False
        session.log("call", "Call ended")
        task = session.monitor
        session.monitor = None
        if task is not None and not task.done():
            task.cancel()
        return session

    def for_code(self, code: str) -> Session | None:
        digits = re.sub(r"\D", "", code or "")
        if len(digits) < 4:
            return None
        return self.by_code.get(digits[-4:])

    def focus(self) -> Session | None:
        sessions = list(self.by_id.values())
        if not sessions:
            return None
        active = [s for s in sessions if s.active]
        pool = active or sessions
        return max(pool, key=lambda s: s.touched)

    def expire(self, ttl_s: float = 6 * 3600) -> None:
        now = time.time()
        for session in list(self.by_id.values()):
            if session.active:
                continue
            if now - session.touched < ttl_s:
                continue
            self.by_id.pop(session.id, None)
            if session.phone and self.by_phone.get(session.phone) is session:
                self.by_phone.pop(session.phone, None)
            if session.code and self.by_code.get(session.code) is session:
                self.by_code.pop(session.code, None)
            if session.call_id and self.by_call.get(session.call_id) is session:
                self.by_call.pop(session.call_id, None)


def _control_url(call: dict) -> str | None:
    monitor = call.get("monitor") or {}
    return monitor.get("controlUrl") or monitor.get("control_url")


STORE = Store()
