#!/usr/bin/env python3
"""Ping each Freq data source and print which ones answered. Never raises."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import USER_AGENT  # noqa: E402

CHECKS = [
    ("adsb.lol point", "https://api.adsb.lol/v2/point/32.8801/-117.2340/10"),
    ("adsb.lol mil", "https://api.adsb.lol/v2/mil"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v3/lat/32.8801/lon/-117.2340/dist/10"),
    (
        "facility map",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/FAA_UAS_FacilityMap_Data/FeatureServer/0/query"
        "?geometry=-117.2340,32.8801&geometryType=esriGeometryPoint&inSR=4326"
        "&spatialRel=esriSpatialRelIntersects&outFields=CEILING&returnGeometry=false&f=json",
    ),
    (
        "class airspace",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Class_Airspace/FeatureServer/0/query"
        "?geometry=-117.2340,32.8801&geometryType=esriGeometryPoint&inSR=4326"
        "&spatialRel=esriSpatialRelIntersects&outFields=NAME,CLASS,LOCAL_TYPE&returnGeometry=false&f=json",
    ),
    (
        "DoD NSUFR",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/DoD_Mar_13/FeatureServer/0/query"
        "?geometry=-117.2150,32.6990&geometryType=esriGeometryPoint&inSR=4326"
        "&spatialRel=esriSpatialRelIntersects&outFields=Facility&returnGeometry=false&f=json",
    ),
    (
        "prohibited",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Prohibited_Areas/FeatureServer/0/query"
        "?where=1%3D1&outFields=NAME&returnGeometry=false&f=json&resultRecordCount=1",
    ),
    (
        "special use",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Special_Use_Airspace/FeatureServer/0/query"
        "?where=1%3D1&outFields=NAME&returnGeometry=false&f=json&resultRecordCount=1",
    ),
    (
        "stadiums",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Stadiums/FeatureServer/0/query"
        "?where=1%3D1&outFields=NAME&returnGeometry=false&f=json&resultRecordCount=1",
    ),
    (
        "defense TFR",
        "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/National_Defense_Airspace_TFR_Areas/FeatureServer/0/query"
        "?where=1%3D1&outFields=NAME&returnGeometry=false&f=json&resultRecordCount=1",
    ),
    ("open-meteo", "https://api.open-meteo.com/v1/elevation?latitude=32.8801&longitude=-117.2340"),
    ("usgs epqs", "https://epqs.nationalmap.gov/v1/json?x=-117.2340&y=32.8801&units=Feet&wkid=4326"),
    ("nws points", "https://api.weather.gov/points/32.8801,-117.2340"),
    ("vapi api", "https://api.vapi.ai/"),
]


def main() -> int:
    live = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=8.0) as client:
        for name, url in CHECKS:
            started = time.perf_counter()
            try:
                response = client.get(url)
                elapsed = time.perf_counter() - started
                ok = response.status_code < 500
                if ok:
                    live += 1
                print(f"{'OK' if ok else 'FAIL':<4} {response.status_code:<4} {elapsed:5.2f}s  {name}")
            except Exception as exc:
                elapsed = time.perf_counter() - started
                print(f"FAIL ---- {elapsed:5.2f}s  {name} ({exc.__class__.__name__})")
    print(f"{live}/{len(CHECKS)} sources responded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
