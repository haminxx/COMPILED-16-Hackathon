"""Scheduled activity near the pilot.

Polygons come from the ceiling index already in memory. The national TFR
list is fetched on the monitor, cached for a few minutes, and filtered down
to California items that mention the San Diego demo. Tool handlers only read
that cache.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import ceiling
from .geo import sm_to_nm, speak_miles
from .sources import get_json

log = logging.getLogger("freq.activities")

TFR_URL = "https://tfr.faa.gov/tfrapi/exportTfrList"
CACHE_S = 180
RETRY_S = 60
NEAR_SM = 10.0

# Description text, not the whole national list. Longer names first so
# "la jolla shores" wins over "la jolla" when both are checked.
LOCAL_PLACES = (
    "la jolla shores",
    "torrey pines",
    "mission bay",
    "point loma",
    "mount soledad",
    "north island",
    "san diego",
    "la jolla",
    "coronado",
    "montgomery",
    "miramar",
    "cabrillo",
    "del mar",
    "pacific beach",
    "ocean beach",
    "imperial beach",
    "solana beach",
)

_lock = asyncio.Lock()
_cache_until = 0.0
_items: list[dict] = []


def due() -> bool:
    return time.time() >= _cache_until


def cached_tfrs() -> list[dict]:
    return list(_items)


def local_tfr_items(rows: list) -> list[dict]:
    kept = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().upper()
        if state not in {"CA", "CALIFORNIA"}:
            continue
        description = " ".join(str(item.get("description") or "").split())
        if not _mentions_demo(description):
            continue
        kept.append(
            {
                "notam_id": item.get("notam_id") or "",
                "type": item.get("type") or "",
                "facility": item.get("facility") or "",
                "state": state,
                "description": description,
                "creation_date": item.get("creation_date") or "",
            }
        )
    return kept


async def refresh_tfr_list() -> None:
    """Fetch the FAA list. A failure leaves the previous cache in place."""
    global _cache_until, _items
    async with _lock:
        if time.time() < _cache_until:
            return
        _cache_until = time.time() + 15
    data = await get_json(TFR_URL, timeout=4.0)
    async with _lock:
        if not isinstance(data, list):
            _cache_until = time.time() + RETRY_S
            log.warning("TFR list unavailable")
            return
        _items = local_tfr_items(data)
        _cache_until = time.time() + CACHE_S
        log.info("TFR list cached, %s San Diego items", len(_items))


def activities_text(session) -> str:
    if session.lat is None or session.lon is None:
        return "I don't have your position, so I can't list what is scheduled nearby."
    sentences = area_sentences(session.lat, session.lon)
    sentences.extend(tfr_sentences(cached_tfrs()))
    if not sentences:
        return "Nothing scheduled nearby."
    return " ".join(sentences[:6])


def area_sentences(lat: float, lon: float, index=None) -> list[str]:
    index = ceiling.current_index() if index is None else index
    if index is None:
        return []
    sentences = []
    for key, builder in (
        ("sua", _sua_sentence),
        ("dod", _dod_sentence),
        ("prohibited", _prohibited_sentence),
        ("nd_tfr", _nd_sentence),
        ("stadiums", _stadium_sentence),
    ):
        for feature, distance_sm, bearing in index.near(key, lat, lon, NEAR_SM):
            sentence = builder(ceiling._props(feature), distance_sm, bearing)
            if sentence and sentence not in sentences:
                sentences.append(sentence)
            if len(sentences) >= 4:
                return sentences
    return sentences


def tfr_sentences(items: list[dict]) -> list[str]:
    lines = []
    for item in items[:2]:
        notam = str(item.get("notam_id") or "").strip()
        kind = str(item.get("type") or "").strip().lower()
        description = str(item.get("description") or "").strip().rstrip(".")
        if not description and not notam:
            continue
        lead = f"Temporary flight restriction {notam}".strip()
        if kind:
            lead += f", {kind},"
        if description:
            lines.append(f"{lead} {description}.")
        else:
            lines.append(f"{lead}.")
    return lines


def _mentions_demo(description: str) -> bool:
    text = description.lower()
    return any(place in text for place in LOCAL_PLACES)


def _sua_sentence(props: dict, distance_sm: float, bearing: str | None) -> str:
    name = str(props.get("NAME") or "special-use airspace").strip()
    label = name if name.lower().startswith("special") else f"Special-use airspace {name}"
    schedule = _schedule(props)
    extra = f"Hours: {schedule}." if schedule else ""
    return _place(label, distance_sm, bearing, extra)


def _dod_sentence(props: dict, distance_sm: float, bearing: str | None) -> str:
    facility = str(props.get("FACILITY") or props.get("BASE") or "a defense site").strip()
    return _place(f"The national security restriction at {facility}", distance_sm, bearing, "")


def _prohibited_sentence(props: dict, distance_sm: float, bearing: str | None) -> str:
    name = str(props.get("NAME") or "a prohibited area").strip()
    return _place(f"Prohibited area {name}", distance_sm, bearing, "")


def _nd_sentence(props: dict, distance_sm: float, bearing: str | None) -> str:
    name = str(props.get("NAME") or props.get("TITLE") or "national defense airspace").strip()
    label = name if "defense" in name.lower() else f"National defense airspace {name}"
    return _place(label, distance_sm, bearing, "")


def _stadium_sentence(props: dict, distance_sm: float, bearing: str | None) -> str:
    name = str(props.get("NAME") or "A stadium").strip()
    return _place(name, distance_sm, bearing, "A game-day stadium TFR can apply.")


def _schedule(props: dict) -> str:
    for key in ("WKHR", "TIMESOFUSE", "TIMES_OF_USE"):
        value = props.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text and text.upper() not in {"NULL", "NONE", "N/A"}:
            if len(text) > 140:
                text = text[:139].rstrip() + "."
            return text
    return ""


def _place(label: str, distance_sm: float, bearing: str | None, extra: str) -> str:
    if distance_sm <= 0.05:
        sentence = f"You are inside {label}."
    else:
        miles = speak_miles(sm_to_nm(distance_sm))
        way = f" {bearing}" if bearing else ""
        sentence = f"{label} is about {miles}{way}."
    if extra:
        sentence = f"{sentence} {extra}".strip()
    return sentence
