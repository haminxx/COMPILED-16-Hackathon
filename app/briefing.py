"""Tool text from cached session state. No network calls."""

from __future__ import annotations

import time

from . import config
from .geo import speak_miles
from .state import Session
from .traffic import situation_traffic


def position_age_phrase(session: Session) -> str:
    age = session.position_age_s
    if age is None or age < 20:
        return ""
    if age < 90:
        return f"Position is {int(age)} seconds old."
    minutes = max(1, int(age // 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Position is {minutes} {unit} old."


def ceiling_text(session: Session) -> str:
    if session.lat is None:
        return (
            "I don't have your position. Open the Freq link and tap Start flight. "
            "If this call is not tied to that page, tell me your 4-digit Freq code."
        )
    if not session.ceiling:
        return _with_demo_fix(session, "Airspace is still loading.")
    reason = session.ceiling.get("reason") or "Airspace is still loading."
    age = position_age_phrase(session)
    return _with_demo_fix(session, f"{reason} {age}".strip())


def _with_demo_fix(session: Session, text: str) -> str:
    if session.loc_source != config.LA_JOLLA_DEMO_SOURCE:
        return text
    note = config.LA_JOLLA_DEMO_NOTE
    if note in text:
        return text
    return f"{text} {note}".strip()


def weather_text(session: Session) -> str:
    if session.weather and session.weather.get("text"):
        return str(session.weather["text"])
    return "Wind is still loading."


def traffic_text(session: Session, radius_nm: float | None = None) -> str:
    rows = session.traffic
    if radius_nm is not None:
        rows = [row for row in rows if row["distance_nm"] <= float(radius_nm)]
    if not rows:
        return "No ADS-B traffic nearby. Freq only knows aircraft that broadcast."
    lines = []
    for row in rows[:8]:
        lines.append(
            f"{row['phrase']} {speak_miles(row['distance_nm'])} {row['bearing']} "
            f"at {row['spoken_alt_ft']} feet"
            + (" closing" if row.get("closing") else "")
        )
    return ". ".join(lines) + ". Only aircraft that broadcast ADS-B are listed."


def situation_text(session: Session) -> str:
    if session.lat is None:
        return ceiling_text(session)
    parts = [ceiling_text(session), situation_traffic(session.traffic, 3), weather_text(session)]
    age = position_age_phrase(session)
    if age and age not in parts[0]:
        parts.append(age)
    return " ".join(part for part in parts if part)


def age_seconds(session: Session) -> int | None:
    if session.loc_ts is None:
        return None
    return int(max(0, time.time() - session.loc_ts))
