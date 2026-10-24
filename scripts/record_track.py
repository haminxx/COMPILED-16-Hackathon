#!/usr/bin/env python3
"""Record a live ADS-B track as offsets from a pilot fix, in the replay schema."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import USER_AGENT  # noqa: E402
from app.geo import local_nm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Record an ADS-B track as pilot-relative offsets.")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--out", type=Path, default=ROOT / "demo" / "recorded_track.json")
    args = parser.parse_args()
    url = f"https://api.adsb.lol/v2/point/{args.lat:.5f}/{args.lon:.5f}/10"
    samples = []
    started = time.time()
    chosen = None
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=8.0, follow_redirects=True) as client:
        while time.time() - started < args.seconds:
            try:
                payload = client.get(url).json()
            except Exception as exc:
                print(f"fetch failed: {exc.__class__.__name__}", file=sys.stderr)
                time.sleep(3)
                continue
            aircraft = payload.get("ac") or []
            pick = _pick(aircraft, chosen)
            if pick is None:
                print("no airborne target yet")
                time.sleep(3)
                continue
            chosen = pick.get("hex")
            north, east = local_nm(args.lat, args.lon, float(pick["lat"]), float(pick["lon"]))
            alt = pick.get("alt_baro")
            samples.append(
                {
                    "t": round(time.time() - started, 1),
                    "north_nm": round(north, 3),
                    "east_nm": round(east, 3),
                    "alt_agl": alt if isinstance(alt, (int, float)) else 700,
                    "gs": pick.get("gs") or 0,
                    "track": pick.get("track") or 0,
                    "category": pick.get("category") or "A7",
                    "flight": pick.get("flight") or "",
                }
            )
            print(f"t={samples[-1]['t']} hex={chosen} n={north:.2f} e={east:.2f}")
            time.sleep(3)
    if len(samples) < 2:
        print("Not enough samples to write a track.", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(samples, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0


def _pick(aircraft: list[dict], chosen: str | None) -> dict | None:
    airborne = []
    for item in aircraft:
        if item.get("lat") is None or item.get("alt_baro") in (None, "ground"):
            continue
        airborne.append(item)
    if chosen:
        for item in airborne:
            if item.get("hex") == chosen:
                return item
    helicopters = [item for item in airborne if str(item.get("category") or "").upper() == "A7"]
    pool = helicopters or airborne
    if not pool:
        return None
    return pool[0]


if __name__ == "__main__":
    sys.exit(main())
