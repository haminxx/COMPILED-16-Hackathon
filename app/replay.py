"""Helicopter replay stored as offsets from the pilot, so any demo spot works."""

from __future__ import annotations

import json
import time

from . import config
from .geo import offset_latlon

_track: list[dict] | None = None


def load_track() -> list[dict]:
    global _track
    if _track is None:
        path = config.DEMO_DIR / "heli_track.json"
        _track = json.loads(path.read_text())
    return _track


def start(session, now: float | None = None) -> None:
    session.replay_t0 = time.time() if now is None else now
    session.log("replay", "Replay helicopter started")


def stop(session) -> None:
    if session.replay_t0 is not None:
        session.log("replay", "Replay helicopter ended")
    session.replay_t0 = None


def sample(session, now: float | None = None) -> dict | None:
    if session.replay_t0 is None or session.lat is None or session.lon is None:
        return None
    track = load_track()
    if not track:
        return None
    elapsed = (time.time() if now is None else now) - session.replay_t0
    if elapsed > float(track[-1]["t"]) + 5:
        stop(session)
        return None
    north, east, alt_agl, gs, track_deg = _interpolate(track, elapsed)
    lat, lon = offset_latlon(session.lat, session.lon, north, east)
    ground = session.ground_ft or 0.0
    return {
        "hex": "FREQH1",
        "flight": "N123HX",
        "category": "A7",
        "lat": lat,
        "lon": lon,
        "alt_baro": float(alt_agl) + float(ground),
        "gs": gs,
        "track": track_deg,
        "seen_pos": 0,
        "replay": True,
    }


def _interpolate(track: list[dict], elapsed: float) -> tuple[float, float, float, float, float]:
    if elapsed <= float(track[0]["t"]):
        row = track[0]
        return _row(row)
    if elapsed >= float(track[-1]["t"]):
        return _row(track[-1])
    for left, right in zip(track, track[1:]):
        t0, t1 = float(left["t"]), float(right["t"])
        if t0 <= elapsed <= t1:
            span = t1 - t0 or 1.0
            mix = (elapsed - t0) / span
            return (
                _lerp(left, right, "north_nm", mix),
                _lerp(left, right, "east_nm", mix),
                _lerp(left, right, "alt_agl", mix),
                _lerp(left, right, "gs", mix),
                _lerp(left, right, "track", mix),
            )
    return _row(track[-1])


def _row(row: dict) -> tuple[float, float, float, float, float]:
    return (
        float(row["north_nm"]),
        float(row["east_nm"]),
        float(row["alt_agl"]),
        float(row["gs"]),
        float(row["track"]),
    )


def _lerp(left: dict, right: dict, key: str, mix: float) -> float:
    return float(left[key]) + (float(right[key]) - float(left[key])) * mix
