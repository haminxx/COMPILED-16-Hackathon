"""Geometry and unit conversion.

Internal distances are nautical miles. Speech and the statute-mile tier
gates convert here so the rest of the app does not mix units.
"""

from __future__ import annotations

import math

NM_PER_DEG_LAT = 60.0
SM_PER_NM = 1.1507794480235425
FT_PER_M = 3.280839895013123
KMH_PER_KNOT = 1.852
M_PER_NM = 1852.0

_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def in_san_diego(lat: float, lon: float) -> bool:
    return 32.53 <= lat <= 33.25 and -117.35 <= lon <= -116.85


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_words(bearing: float) -> str:
    idx = int((bearing + 22.5) // 45) % 8
    return _COMPASS[idx]


def nm_to_sm(nm: float) -> float:
    return nm * SM_PER_NM


def sm_to_nm(sm: float) -> float:
    return sm / SM_PER_NM


def meters_to_ft(meters: float) -> float:
    return meters * FT_PER_M


def kmh_to_knots(kmh: float) -> float:
    return kmh / KMH_PER_KNOT


def nm_to_meters(nm: float) -> float:
    return nm * M_PER_NM


def round_alt_ft(feet: float) -> int:
    return int(round(float(feet) / 100.0) * 100)


def speak_miles(nm: float) -> str:
    sm = nm_to_sm(nm)
    if sm < 0.5:
        return "less than a mile"
    if sm < 10:
        rounded = round(sm, 1)
        if abs(rounded - round(rounded)) < 0.05:
            n = int(round(rounded))
            return "1 mile" if n == 1 else f"{n} miles"
        return f"{rounded:.1f} miles"
    n = int(round(sm))
    return "1 mile" if n == 1 else f"{n} miles"


def speak_knots(knots: float) -> str:
    n = int(round(knots))
    return "1 knot" if n == 1 else f"{n} knots"


def format_wind(speed_kmh: float | None, gust_kmh: float | None, direction_deg: float | None) -> str:
    """NWS windSpeed / windGust are km/h. windGust may be null."""
    if speed_kmh is None:
        return "Wind unavailable."
    direction = compass_words(direction_deg) if direction_deg is not None else "variable"
    text = f"Wind {direction} {speak_knots(kmh_to_knots(speed_kmh))}"
    if gust_kmh is not None:
        text += f", gusts {speak_knots(kmh_to_knots(gust_kmh))}"
    return text + "."


def offset_latlon(lat: float, lon: float, north_nm: float, east_nm: float) -> tuple[float, float]:
    dlat = north_nm / NM_PER_DEG_LAT
    cos_lat = math.cos(math.radians(lat))
    dlon = 0.0 if abs(cos_lat) < 1e-8 else east_nm / (NM_PER_DEG_LAT * cos_lat)
    return lat + dlat, lon + dlon


def local_nm(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Aircraft minus pilot, in nautical miles north and east."""
    north = (lat - lat0) * NM_PER_DEG_LAT
    east = (lon - lon0) * NM_PER_DEG_LAT * math.cos(math.radians(lat0))
    return north, east


def cpa_nm_s(
    north_nm: float,
    east_nm: float,
    track_deg: float | None,
    gs_knots: float | None,
) -> tuple[float, float] | None:
    """Closest approach of a moving aircraft to a stationary pilot.

    Returns (cpa_nm, seconds). Seconds are negative when the closest point
    is already behind the aircraft. None when track or speed is missing.
    """
    if track_deg is None or gs_knots is None:
        return None
    try:
        gs = float(gs_knots)
        track = float(track_deg)
    except (TypeError, ValueError):
        return None
    if gs <= 1.0:
        return None
    rad = math.radians(track)
    vn = gs * math.cos(rad)
    ve = gs * math.sin(rad)
    speed2 = vn * vn + ve * ve
    if speed2 <= 1e-6:
        return None
    t_hours = -(north_nm * vn + east_nm * ve) / speed2
    cpa_n = north_nm + vn * t_hours
    cpa_e = east_nm + ve * t_hours
    return math.hypot(cpa_n, cpa_e), t_hours * 3600.0
