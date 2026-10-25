"""Runtime settings. Secrets come from the environment or freq .env — never from code."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


VAPI_API_KEY = _env("VAPI_API_KEY")
VAPI_PHONE_NUMBER_ID = _env("VAPI_PHONE_NUMBER_ID")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WEBHOOK_TOKEN = _env("WEBHOOK_TOKEN", "dev-webhook-token")
PHONE_DISPLAY = _env("PHONE_DISPLAY", "+1 858-348-9143")
DEMO_MODE = _env("DEMO_MODE").lower()
DEMO_LOCATION = _env("DEMO_LOCATION")
DEMO_TFR = _flag("DEMO_TFR")
OFFLINE = _flag("FREQ_OFFLINE")
# Used only when a Vapi call arrives with no coordinates. The ceiling still
# comes from the live engine; this sentence marks the stand-in fix.
LA_JOLLA_DEMO_SOURCE = "la-jolla-cove"
LA_JOLLA_DEMO_NOTE = "This demo fix is La Jolla Cove, not the pilot's GPS."

USER_AGENT = "Freq/0.1 (hackathon)"
ASSISTANT_NAME = "Freq"

# San Diego demo box. lat min/max, lon min/max.
LAT_MIN, LAT_MAX = 32.53, 33.25
LON_MIN, LON_MAX = -117.35, -116.85
# ArcGIS envelope: xmin, ymin, xmax, ymax (lon, lat, lon, lat).
ENVELOPE = f"{LON_MIN},{LAT_MIN},{LON_MAX},{LAT_MAX}"

CACHE_DIR = ROOT / "data" / "cache"
DEMO_DIR = ROOT / "demo"
STATIC_DIR = ROOT / "static"

TEL_HREF = "tel:+18583489143"
OUTSIDE_BOX = "Freq demo covers San Diego only."

# Recreational defaults. Distance gates are statute miles so the dashboard
# and the voice line use the same numbers. Geometry stays in nautical miles.
CLASS_G_FT = 400
GLIDER_ADVISORY_FT = 200
ALERT_RANGE_SM = 3.0
DRIP_RANGE_SM = 5.0
LAND_RANGE_SM = 1.5
CPA_MAX_SM = 1.0
TCPA_MAX_S = 120.0
ALERT_AGL_FT = 1500
DRIP_AGL_FT = 2500
LAND_AGL_FT = 500
DRIP_SKIP_CATEGORIES = frozenset({"A3", "A4", "A5"})
DRIP_SKIP_ABOVE_AGL = 1000
COOLDOWN_S = 45.0
INJECTION_GAP_S = 8.0
ALL_CLEAR_S = 90.0
MOVE_RECOMPUTE_M = 300.0
ADSB_RADIUS_NM = 10
STALE_POS_S = 15.0
