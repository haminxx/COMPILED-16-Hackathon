"""ADS-B traffic, CPA closing, and the drip / alert tiers.

Speech is statute miles, knots, and altitudes rounded to 100 ft.
Tail numbers are never copied into an injection.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from . import config
from .geo import (
    bearing_deg,
    compass_words,
    cpa_nm_s,
    haversine_nm,
    local_nm,
    nm_to_sm,
    round_alt_ft,
    speak_miles,
)

log = logging.getLogger("freq.traffic")

_point_cache: dict[tuple[float, float], tuple[float, list]] = {}
_mil_cache: tuple[float, list] | None = None
_flip = 0

TYPE_ALIASES = {
    "helicopter": ["A7"],
    "heli": ["A7"],
    "light": ["A1"],
    "small": ["A2"],
    "airliner": ["A3", "A4", "A5"],
    "airline": ["A3", "A4", "A5"],
    "large": ["A3"],
    "heavy": ["A4", "A5"],
    "ga": ["A1", "A2"],
}

PHRASES = {
    "A1": "A light aircraft",
    "A2": "A small aircraft",
    "A3": "A large aircraft",
    "A4": "A heavy aircraft",
    "A5": "A heavy aircraft",
    "A7": "A helicopter",
}


@dataclass
class Injection:
    tier: str
    aircraft_id: str
    kind: str
    content: str
    interrupt: bool


def blank_memory() -> dict[str, Any]:
    return {"cooldowns": {}, "ranges": {}, "last_injection": None}


def default_rules() -> dict[str, Any]:
    return {
        "radius_nm": None,
        "max_alt_ft": None,
        "include_types": [],
        "exclude_types": [],
        "quiet": False,
    }


def normalize_types(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        parts = [str(part).strip() for part in value]
    else:
        parts = [str(value).strip()]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        key = part.upper()
        if key in PHRASES or (len(key) == 2 and key[0] == "A"):
            out.append(key)
            continue
        out.extend(TYPE_ALIASES.get(part.lower(), []))
    return out


def merge_rules(current: dict | None, patch: dict) -> dict:
    rules = dict(current or default_rules())
    if "radius_nm" in patch and patch["radius_nm"] not in (None, ""):
        rules["radius_nm"] = float(patch["radius_nm"])
    if "max_alt_ft" in patch and patch["max_alt_ft"] not in (None, ""):
        rules["max_alt_ft"] = float(patch["max_alt_ft"])
    if "include_types" in patch and patch["include_types"] not in (None, ""):
        rules["include_types"] = normalize_types(patch["include_types"])
    if "exclude_types" in patch and patch["exclude_types"] not in (None, ""):
        rules["exclude_types"] = normalize_types(patch["exclude_types"])
    if "quiet" in patch and patch["quiet"] not in (None, ""):
        rules["quiet"] = _truthy(patch["quiet"])
    return rules


def confirm_rules(rules: dict) -> str:
    bits = ["Rule set."]
    if rules.get("quiet"):
        bits.append("Quiet unless urgent.")
    if rules.get("radius_nm") is not None:
        bits.append(f"Radius {rules['radius_nm']} nautical miles.")
    if rules.get("max_alt_ft") is not None:
        bits.append(f"Ignore aircraft above {int(rules['max_alt_ft'])} feet AGL.")
    if rules.get("include_types"):
        bits.append("Only " + ", ".join(rules["include_types"]) + ".")
    if rules.get("exclude_types"):
        bits.append("Exclude " + ", ".join(rules["exclude_types"]) + ".")
    return " ".join(bits)


def usable(raw: dict) -> bool:
    seen = raw.get("seen_pos")
    if seen is not None and seen != "":
        try:
            if float(seen) > config.STALE_POS_S:
                return False
        except (TypeError, ValueError):
            pass
    if _alt_ft(raw.get("alt_baro")) is None:
        return False
    if raw.get("lat") is None or raw.get("lon") is None:
        return False
    return True


def prepare(raw_list: list[dict], pilot_lat: float, pilot_lon: float, ground_ft: float | None) -> list[dict]:
    ground = 0.0 if ground_ft is None else float(ground_ft)
    prepared = []
    for raw in raw_list:
        if not usable(raw):
            continue
        try:
            lat = float(raw["lat"])
            lon = float(raw["lon"])
        except (TypeError, ValueError):
            continue
        alt = _alt_ft(raw.get("alt_baro"))
        if alt is None:
            continue
        agl = alt - ground
        north, east = local_nm(pilot_lat, pilot_lon, lat, lon)
        distance = haversine_nm(pilot_lat, pilot_lon, lat, lon)
        category = str(raw.get("category") or "").upper()
        aircraft_id = str(raw.get("hex") or raw.get("id") or f"{lat:.4f},{lon:.4f}")
        item = {
            "id": aircraft_id,
            "category": category,
            "phrase": _phrase(category, bool(raw.get("mil"))),
            "lat": lat,
            "lon": lon,
            "distance_nm": distance,
            "bearing": compass_words(bearing_deg(pilot_lat, pilot_lon, lat, lon)),
            "agl_ft": agl,
            "alt_msl_ft": alt,
            "spoken_alt_ft": round_alt_ft(agl),
            "gs": _optional_float(raw.get("gs")),
            "track": _optional_float(raw.get("track")),
            "north_nm": north,
            "east_nm": east,
            "replay": bool(raw.get("replay")),
            # Kept for debugging. Formatters must not read these.
            "flight": raw.get("flight"),
            "hex": raw.get("hex"),
        }
        prepared.append(item)
    prepared.sort(key=lambda item: item["distance_nm"])
    return prepared


def choose(aircraft: list[dict], memory: dict, rules: dict | None, now: float) -> Injection | None:
    """Pick at most one injection. Does not stamp cooldowns (see commit)."""
    rules = rules or default_rules()
    memory.setdefault("cooldowns", {})
    memory.setdefault("ranges", {})
    alerts: list[tuple[int, dict]] = []
    drips: list[dict] = []
    for ac in aircraft:
        closing = is_closing(ac, memory)
        ac["closing"] = closing
        if not _passes_type(ac, rules):
            continue
        if _in_alert(ac, rules) and closing:
            rank = 0 if _land_now(ac) else 1
            alerts.append((rank, ac))
        elif _in_drip(ac, rules) and _passes_drip_category(ac, rules):
            drips.append(ac)
    _remember_ranges(memory, aircraft)
    if _throttled(memory, now):
        return None

    if alerts:
        alerts.sort(key=lambda pair: (pair[0], pair[1]["distance_nm"]))
        for _rank, ac in alerts:
            tier = "land" if _land_now(ac) else "alert"
            if _on_cooldown(memory, ac["id"], tier, now):
                continue
            return _injection(ac, tier)
        return None

    if rules.get("quiet"):
        return None
    drips.sort(key=lambda ac: ac["distance_nm"])
    for ac in drips:
        if _on_cooldown(memory, ac["id"], "drip", now):
            continue
        return _injection(ac, "drip")
    return None


def commit(memory: dict, injection: Injection, now: float) -> None:
    memory.setdefault("cooldowns", {})
    memory["cooldowns"][(injection.aircraft_id, injection.tier)] = now
    memory["last_injection"] = now


def is_closing(ac: dict, memory: dict) -> bool:
    """CPA when track and groundspeed exist; otherwise two decreasing ranges."""
    result = cpa_nm_s(ac["north_nm"], ac["east_nm"], ac.get("track"), ac.get("gs"))
    if result is not None:
        cpa_nm, tcpa_s = result
        ac["cpa_nm"] = cpa_nm
        ac["tcpa_s"] = tcpa_s
        return tcpa_s >= 0 and nm_to_sm(cpa_nm) <= config.CPA_MAX_SM and tcpa_s <= config.TCPA_MAX_S
    previous = memory.get("ranges", {}).get(ac["id"])
    if previous is None:
        return False
    return ac["distance_nm"] < float(previous) - 0.02


def speak_aircraft(ac: dict, *, closing: bool, land: bool) -> str:
    miles = speak_miles(ac["distance_nm"])
    alt = ac.get("spoken_alt_ft")
    if alt is None:
        alt = round_alt_ft(ac["agl_ft"])
    core = f"{ac['phrase']} {miles} {ac['bearing']}, {alt} feet"
    if land:
        return f"{core}. Land now."
    if closing:
        return f"{core}, closing. Descend to 100 feet or below and hold."
    return f"{core}."


def situation_traffic(aircraft: list[dict], limit: int = 3) -> str:
    if not aircraft:
        return "No ADS-B traffic nearby."
    lines = []
    for ac in aircraft[:limit]:
        lines.append(
            f"{ac['phrase']} {speak_miles(ac['distance_nm'])} {ac['bearing']} at {ac['spoken_alt_ft']} feet"
        )
    return "Traffic: " + "; ".join(lines) + "."


async def fetch_live(lat: float, lon: float) -> list[dict]:
    """Point feed and military feed, cached, at most one ADS-B request per second."""
    global _flip, _mil_cache
    from .sources import adsb_slot, get_json

    _flip += 1
    point = _cached_point(lat, lon)
    if _flip % 2 == 1 or point is None:
        await adsb_slot()
        point = await _fetch_point(lat, lon, get_json)
        _store_point(lat, lon, point)
    else:
        await adsb_slot()
        mil = await _fetch_mil(get_json)
        _mil_cache = (time.monotonic(), mil)

    mil_list = _mil_cache[1] if _mil_cache else []
    merged: dict[str, dict] = {}
    for raw in point or []:
        key = str(raw.get("hex") or id(raw))
        merged[key] = raw
    for raw in mil_list:
        if raw.get("lat") is None or raw.get("lon") is None:
            continue
        try:
            if haversine_nm(lat, lon, float(raw["lat"]), float(raw["lon"])) > config.ADSB_RADIUS_NM:
                continue
        except (TypeError, ValueError):
            continue
        tagged = dict(raw)
        tagged["mil"] = True
        key = str(tagged.get("hex") or id(tagged))
        merged.setdefault(key, tagged)
    return list(merged.values())


def _injection(ac: dict, tier: str) -> Injection:
    land = tier == "land"
    closing = tier in {"alert", "land"}
    sentence = speak_aircraft(ac, closing=closing, land=land)
    if tier == "drip":
        content = (
            f"ADVISORY: {ac['phrase']} {speak_miles(ac['distance_nm'])} {ac['bearing']} "
            f"at {ac['spoken_alt_ft']} feet. Tell the pilot in one sentence. "
            "Do not mention a tail number."
        )
        return Injection(tier="drip", aircraft_id=ac["id"], kind="add", content=content, interrupt=False)
    return Injection(tier=tier, aircraft_id=ac["id"], kind="say", content=sentence, interrupt=True)


def _in_alert(ac: dict, rules: dict) -> bool:
    drip_sm, alert_sm, drip_agl, alert_agl = _gates(rules)
    if nm_to_sm(ac["distance_nm"]) > alert_sm:
        return False
    if ac["agl_ft"] > alert_agl:
        return False
    return True


def _in_drip(ac: dict, rules: dict) -> bool:
    drip_sm, _alert_sm, drip_agl, _alert_agl = _gates(rules)
    if nm_to_sm(ac["distance_nm"]) > drip_sm:
        return False
    if ac["agl_ft"] > drip_agl:
        return False
    return True


def _land_now(ac: dict) -> bool:
    return nm_to_sm(ac["distance_nm"]) <= config.LAND_RANGE_SM and ac["agl_ft"] <= config.LAND_AGL_FT


def _gates(rules: dict) -> tuple[float, float, float, float]:
    drip_sm = config.DRIP_RANGE_SM
    alert_sm = config.ALERT_RANGE_SM
    if rules.get("radius_nm") is not None:
        drip_sm = nm_to_sm(float(rules["radius_nm"]))
        alert_sm = min(alert_sm, drip_sm)
    alert_agl = float(config.ALERT_AGL_FT)
    drip_agl = float(config.DRIP_AGL_FT)
    if rules.get("max_alt_ft") is not None:
        cap = float(rules["max_alt_ft"])
        alert_agl = min(alert_agl, cap)
        drip_agl = min(drip_agl, cap)
    return drip_sm, alert_sm, drip_agl, alert_agl


def _passes_type(ac: dict, rules: dict) -> bool:
    cat = (ac.get("category") or "").upper()
    include = set(rules.get("include_types") or [])
    exclude = set(rules.get("exclude_types") or [])
    if include and cat not in include:
        return False
    if exclude and cat in exclude:
        return False
    return True


def _passes_drip_category(ac: dict, rules: dict) -> bool:
    if rules.get("quiet"):
        return False
    cat = (ac.get("category") or "").upper()
    include = set(rules.get("include_types") or [])
    if cat in config.DRIP_SKIP_CATEGORIES and ac["agl_ft"] > config.DRIP_SKIP_ABOVE_AGL:
        if cat not in include:
            return False
    return True


def _on_cooldown(memory: dict, aircraft_id: str, tier: str, now: float) -> bool:
    stamp = memory.get("cooldowns", {}).get((aircraft_id, tier))
    if stamp is None:
        return False
    return (now - float(stamp)) < config.COOLDOWN_S


def _throttled(memory: dict, now: float) -> bool:
    last = memory.get("last_injection")
    if last is None:
        return False
    return (now - float(last)) < config.INJECTION_GAP_S


def _remember_ranges(memory: dict, aircraft: list[dict]) -> None:
    ranges = memory.setdefault("ranges", {})
    for ac in aircraft:
        ranges[ac["id"]] = ac["distance_nm"]


def _phrase(category: str, mil: bool) -> str:
    if category in PHRASES:
        return PHRASES[category]
    if mil:
        return "A military aircraft"
    return "An aircraft"


def _alt_ft(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() == "ground":
            return None
        value = value.strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat / 0.05) * 0.05, round(lon / 0.05) * 0.05)


def _cached_point(lat: float, lon: float) -> list | None:
    hit = _point_cache.get(_cell(lat, lon))
    if not hit:
        return None
    stamp, payload = hit
    if time.monotonic() - stamp > 3.0:
        return None
    return payload


def _store_point(lat: float, lon: float, payload: list) -> None:
    _point_cache[_cell(lat, lon)] = (time.monotonic(), payload)


async def _fetch_point(lat: float, lon: float, get_json) -> list:
    primary = f"https://api.adsb.lol/v2/point/{lat:.5f}/{lon:.5f}/{config.ADSB_RADIUS_NM}"
    data = await get_json(primary, timeout=3.0)
    if not isinstance(data, dict):
        fallback = (
            f"https://opendata.adsb.fi/api/v3/lat/{lat:.5f}/lon/{lon:.5f}/dist/{config.ADSB_RADIUS_NM}"
        )
        data = await get_json(fallback, timeout=3.0)
    if not isinstance(data, dict):
        return []
    ac = data.get("ac") or []
    return ac if isinstance(ac, list) else []


async def _fetch_mil(get_json) -> list:
    data = await get_json("https://api.adsb.lol/v2/mil", timeout=3.0)
    if not isinstance(data, dict):
        return []
    ac = data.get("ac") or []
    return ac if isinstance(ac, list) else []
