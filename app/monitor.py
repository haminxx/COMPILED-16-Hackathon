"""Per-call loop. External fetches happen here, never inside a Vapi tool handler."""

from __future__ import annotations

import asyncio
import logging
import time

from . import ceiling, config, replay, traffic, vapi_control
from .geo import format_wind, haversine_nm, nm_to_meters
from .sources import get_json
from .state import Session

log = logging.getLogger("freq.monitor")

_elev_cache: dict[tuple[float, float], float] = {}
_epqs_tried: set[tuple[float, float]] = set()
_wind_cache: dict[tuple[float, float], tuple[float, dict]] = {}


def ensure(session: Session) -> None:
    task = session.monitor
    if task is not None and not task.done():
        return
    session.monitor = asyncio.create_task(_loop(session), name=f"freq-{session.code or session.id}")


async def _loop(session: Session) -> None:
    try:
        while session.active:
            try:
                await tick(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick failed")
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        return


async def tick(session: Session) -> None:
    if session.lat is None or session.lon is None:
        return
    await _refresh_ceiling(session)
    session.ground_ft = await ground_ft(session.lat, session.lon)
    if session.weather is None or time.time() - session.weather_ts > 600:
        wind = await wind_obs(session.lat, session.lon)
        if wind is not None:
            session.weather = wind
            session.weather_ts = time.time()
    raw = []
    try:
        raw = await traffic.fetch_live(session.lat, session.lon)
    except Exception:
        log.warning("traffic fetch failed", exc_info=True)
    ghost = replay.sample(session)
    if ghost is not None:
        raw = list(raw) + [ghost]
    prepared = traffic.prepare(raw, session.lat, session.lon, session.ground_ft)
    now = time.monotonic()
    injection = traffic.choose(prepared, session.memory, session.rules, now)
    session.traffic = prepared
    if injection is not None and session.control_url:
        ok = await vapi_control.dispatch(
            session.control_url,
            injection.kind,
            injection.content,
            interrupt=injection.interrupt,
        )
        if ok:
            traffic.commit(session.memory, injection, now)
            session.last_all_clear = None
            session.log(injection.tier, injection.content)
            return
    elif injection is not None:
        return
    await _maybe_all_clear(session, prepared, now)


async def _refresh_ceiling(session: Session) -> None:
    if session.lat is None or session.lon is None:
        return
    anchor = session.ceiling_anchor
    needs_note = session.loc_source == config.LA_JOLLA_DEMO_SOURCE
    has_note = config.LA_JOLLA_DEMO_NOTE in ((session.ceiling or {}).get("reason") or "")
    if anchor is not None and session.ceiling is not None and needs_note == has_note:
        moved_m = nm_to_meters(haversine_nm(anchor[0], anchor[1], session.lat, session.lon))
        if moved_m < config.MOVE_RECOMPUTE_M:
            return
    result = ceiling.ceiling_here(session.lat, session.lon)
    payload = result.to_dict()
    if needs_note and config.LA_JOLLA_DEMO_NOTE not in payload["reason"]:
        payload["reason"] = f"{payload['reason']} {config.LA_JOLLA_DEMO_NOTE}".strip()
    session.ceiling = payload
    if result.queried:
        session.ceiling_anchor = (session.lat, session.lon)
        session.log("ceiling", payload["reason"])


async def _maybe_all_clear(session: Session, aircraft: list[dict], now: float) -> None:
    if session.rules.get("quiet") or not session.control_url or not session.ceiling:
        return
    if not session.ceiling.get("queried"):
        return
    if any(ac.get("closing") and _could_alert(ac) for ac in aircraft):
        return
    if any(_could_drip(ac) for ac in aircraft):
        return
    anchor = session.memory.get("last_injection")
    wall_anchor = session.call_started
    if wall_anchor is None:
        return
    if time.time() - wall_anchor < config.ALL_CLEAR_S:
        return
    if anchor is not None and now - float(anchor) < config.ALL_CLEAR_S:
        return
    if session.last_all_clear is not None and time.time() - session.last_all_clear < config.ALL_CLEAR_S:
        return
    text = _all_clear_text(session.ceiling)
    ok = await vapi_control.say(session.control_url, text, interrupt=False)
    if ok:
        session.memory["last_injection"] = now
        session.last_all_clear = time.time()
        session.log("all-clear", text)


def _could_alert(ac: dict) -> bool:
    from .geo import nm_to_sm

    return nm_to_sm(ac["distance_nm"]) <= config.ALERT_RANGE_SM and ac["agl_ft"] <= config.ALERT_AGL_FT


def _could_drip(ac: dict) -> bool:
    from .geo import nm_to_sm

    if nm_to_sm(ac["distance_nm"]) > config.DRIP_RANGE_SM or ac["agl_ft"] > config.DRIP_AGL_FT:
        return False
    cat = (ac.get("category") or "").upper()
    if cat in config.DRIP_SKIP_CATEGORIES and ac["agl_ft"] > config.DRIP_SKIP_ABOVE_AGL:
        return False
    return True


def _all_clear_text(ceiling_dict: dict) -> str:
    if ceiling_dict.get("needs_laanc") and (ceiling_dict.get("max_alt_ft_agl") or 0) <= 0:
        return "All clear of traffic. Don't fly here."
    if ceiling_dict.get("needs_laanc"):
        return f"All clear of traffic. LAANC ceiling is {ceiling_dict.get('max_alt_ft_agl')} feet."
    altitude = ceiling_dict.get("max_alt_ft_agl")
    if altitude is None:
        return "All clear of ADS-B traffic."
    return f"All clear, ceiling {altitude} feet."


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 2), round(lon, 2))


async def ground_ft(lat: float, lon: float) -> float:
    cell = _cell(lat, lon)
    if cell in _elev_cache:
        if cell not in _epqs_tried:
            asyncio.create_task(_refine_epqs(lat, lon, cell))
        return _elev_cache[cell]
    meters = await _open_meteo(lat, lon)
    if meters is not None:
        _elev_cache[cell] = meters
        asyncio.create_task(_refine_epqs(lat, lon, cell))
        return meters
    refined = await _epqs(lat, lon)
    _epqs_tried.add(cell)
    if refined is not None:
        _elev_cache[cell] = refined
        return refined
    _elev_cache[cell] = 0.0
    return 0.0


async def _open_meteo(lat: float, lon: float) -> float | None:
    from .geo import meters_to_ft

    data = await get_json(
        "https://api.open-meteo.com/v1/elevation",
        params={"latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}"},
        timeout=3.0,
    )
    if not isinstance(data, dict):
        return None
    values = data.get("elevation") or []
    if not values:
        return None
    try:
        return meters_to_ft(float(values[0]))
    except (TypeError, ValueError):
        return None


async def _refine_epqs(lat: float, lon: float, cell: tuple[float, float]) -> None:
    if cell in _epqs_tried:
        return
    _epqs_tried.add(cell)
    value = await _epqs(lat, lon)
    if value is not None:
        _elev_cache[cell] = value


async def _epqs(lat: float, lon: float) -> float | None:
    data = await get_json(
        "https://epqs.nationalmap.gov/v1/json",
        params={"x": f"{lon:.5f}", "y": f"{lat:.5f}", "units": "Feet", "wkid": "4326"},
        timeout=3.0,
    )
    if not isinstance(data, dict) or "value" not in data:
        return None
    try:
        value = float(data["value"])
    except (TypeError, ValueError):
        return None
    if value < -1000 or value > 20000:
        return None
    return value


async def wind_obs(lat: float, lon: float) -> dict | None:
    cell = (round(lat / 0.05) * 0.05, round(lon / 0.05) * 0.05)
    cached = _wind_cache.get(cell)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    points = await get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", timeout=3.0)
    if not isinstance(points, dict):
        return None
    stations_url = ((points.get("properties") or {}).get("observationStations"))
    if not stations_url:
        return None
    listing = await get_json(stations_url, timeout=3.0)
    features = (listing or {}).get("features") if isinstance(listing, dict) else None
    if not features:
        return None
    station = ((features[0].get("properties") or {}).get("stationIdentifier"))
    if not station:
        return None
    obs = await get_json(f"https://api.weather.gov/stations/{station}/observations/latest", timeout=3.0)
    if not isinstance(obs, dict):
        return None
    props = obs.get("properties") or {}
    speed = _quantity(props.get("windSpeed"))
    gust = _quantity(props.get("windGust"))
    direction = _quantity(props.get("windDirection"))
    if speed is None and gust is None:
        return None
    parsed = {"speed_kmh": speed, "gust_kmh": gust, "dir_deg": direction, "text": format_wind(speed, gust, direction)}
    _wind_cache[cell] = (time.time(), parsed)
    return parsed


def _quantity(field) -> float | None:
    if not isinstance(field, dict):
        return None
    value = field.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
