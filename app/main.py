"""Freq HTTP app: start page, dashboard, location, replay, and the Vapi webhook."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ceiling, config, monitor, replay, vapi_webhook
from .briefing import age_seconds
from .geo import in_san_diego
from .sources import aclose
from .state import STORE, normalize_phone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("freq")

_debug: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    ceiling.load_local_demo()
    task = asyncio.create_task(_boot_airspace())
    yield
    task.cancel()
    for session in list(STORE.by_id.values()):
        if session.monitor is not None and not session.monitor.done():
            session.monitor.cancel()
    await aclose()


app = FastAPI(title="Freq", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


class LocIn(BaseModel):
    phone: str
    lat: float
    lon: float
    accuracy: float | None = None


class SpotIn(BaseModel):
    name: str
    phone: str | None = None


class ReplayIn(BaseModel):
    phone: str | None = None


class CodeIn(BaseModel):
    phone: str = Field(min_length=7)


@app.get("/")
async def root():
    return FileResponse(config.STATIC_DIR / "start.html")


@app.get("/start")
async def start_page():
    return FileResponse(config.STATIC_DIR / "start.html")


@app.get("/dash")
async def dash_page():
    return FileResponse(config.STATIC_DIR / "dash.html")


@app.get("/health")
async def health():
    index = ceiling.current_index()
    return {
        "ok": True,
        "name": "Freq",
        "layers_ready": bool(index and index.ready),
    }


@app.post("/api/session")
async def create_session(body: CodeIn):
    session = STORE.ensure_phone(body.phone)
    _maybe_pin_demo(session)
    return _session_public(session)


@app.post("/api/loc")
async def post_loc(body: LocIn):
    if not normalize_phone(body.phone):
        raise HTTPException(status_code=400, detail="Phone number needs at least 10 digits.")
    session = STORE.ensure_phone(body.phone)
    # A spot pin holds until the phone sends a fix. The phone fix is the takeoff point.
    session.lat = body.lat
    session.lon = body.lon
    session.accuracy = body.accuracy
    session.loc_ts = time.time()
    session.loc_source = "phone"
    session.loc_pinned = False
    session.touch()
    if session.active:
        monitor.ensure(session)
    return _session_public(session)


@app.get("/api/state")
async def state():
    session = STORE.focus()
    index = ceiling.current_index()
    return {
        "phone_display": config.PHONE_DISPLAY,
        "layers_ready": bool(index and index.ready),
        "spots": ceiling.spots(),
        "session": None if session is None else _session_public(session, detail=True),
    }


@app.post("/api/spot")
async def pick_spot(body: SpotIn):
    match = next((item for item in ceiling.spots() if item["name"].lower() == body.name.lower()), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown spot")
    session = STORE.ensure_phone(body.phone) if body.phone else STORE.focus()
    if session is None:
        session = STORE.ensure_phone("0000000000")
    session.lat = float(match["lat"])
    session.lon = float(match["lon"])
    session.loc_ts = time.time()
    session.loc_source = "simulated"
    session.loc_pinned = True
    session.accuracy = None
    session.ceiling = None
    session.ceiling_anchor = None
    session.log("location", f"Spot {match['name']}")
    if session.active:
        monitor.ensure(session)
    return _session_public(session, detail=True)


@app.post("/api/replay/start")
async def replay_start(body: ReplayIn | None = None):
    phone = body.phone if body else None
    session = STORE.ensure_phone(phone) if phone else STORE.focus()
    if session is None:
        raise HTTPException(status_code=400, detail="Open the start page so Freq has a session.")
    if session.lat is None:
        _maybe_pin_demo(session)
    if session.lat is None:
        raise HTTPException(status_code=400, detail="Need a position before replay.")
    replay.start(session)
    if session.active:
        monitor.ensure(session)
    return {"ok": True, "code": session.code, "replay": True}


@app.get("/api/layers")
async def layers():
    index = ceiling.current_index()
    advisories = ceiling.current_advisories()
    collections = {}
    if index is not None:
        for key, features in index.layers.items():
            collections[key] = {"type": "FeatureCollection", "features": features}
    else:
        collections = {key: {"type": "FeatureCollection", "features": []} for key in (
            "facility", "class_airspace", "dod", "prohibited", "sua", "stadiums", "nd_tfr"
        )}
    return {
        "layers": collections,
        "ready": bool(index and index.ready),
        "glider": _geom_json(advisories.glider),
        "demo_tfr": _geom_json(advisories.demo_tfr) if advisories.demo_tfr_enabled else None,
    }


@app.post("/vapi/webhook/{token}")
async def webhook(token: str, request: Request):
    if not vapi_webhook.authorized(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        body = await vapi_webhook.handle(payload if isinstance(payload, dict) else {})
    except Exception:
        log.exception("webhook failed")
        body = {"ok": True}
    return JSONResponse(body)


@app.post("/debug/control")
async def debug_control(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    _debug.append({"t": time.time(), "body": body})
    del _debug[:-50]
    return {"ok": True}


@app.get("/debug/control")
async def debug_control_log():
    return {"injections": _debug}


def _maybe_pin_demo(session) -> None:
    if session.lat is not None or not config.DEMO_LOCATION:
        return
    parts = [part.strip() for part in config.DEMO_LOCATION.split(",")]
    if len(parts) != 2:
        return
    try:
        session.lat = float(parts[0])
        session.lon = float(parts[1])
    except ValueError:
        return
    session.loc_source = "simulated"
    session.loc_ts = time.time()


def _session_public(session, *, detail: bool = False) -> dict:
    payload = {
        "code": session.code,
        "phone": session.phone,
        "active": session.active,
        "lat": session.lat,
        "lon": session.lon,
        "accuracy": session.accuracy,
        "position_age_s": age_seconds(session),
        "loc_source": session.loc_source,
        "in_bounds": None if session.lat is None else in_san_diego(session.lat, session.lon),
        "replay": session.replay_active,
    }
    if detail:
        payload.update(
            {
                "ceiling": session.ceiling,
                "weather": session.weather,
                "traffic": [_public_aircraft(item) for item in session.traffic],
                "events": list(session.events)[-40:],
                "rules": session.rules,
            }
        )
    return payload


def _public_aircraft(item: dict) -> dict:
    """Dashboard fields. Tail numbers stay off the wire."""
    return {
        "id": item.get("id"),
        "phrase": item.get("phrase"),
        "category": item.get("category"),
        "lat": item.get("lat"),
        "lon": item.get("lon"),
        "distance_nm": item.get("distance_nm"),
        "bearing": item.get("bearing"),
        "agl_ft": item.get("agl_ft"),
        "spoken_alt_ft": item.get("spoken_alt_ft"),
        "closing": item.get("closing"),
        "cpa_nm": item.get("cpa_nm"),
        "tcpa_s": item.get("tcpa_s"),
        "replay": item.get("replay"),
        "north_nm": item.get("north_nm"),
        "east_nm": item.get("east_nm"),
    }


def _geom_json(geom) -> dict | None:
    if geom is None:
        return None
    try:
        from shapely.geometry import mapping
        return mapping(geom)
    except Exception:
        return None


async def _boot_airspace() -> None:
    try:
        log.info("loading San Diego airspace polygons")
        if config.OFFLINE:
            index = ceiling.load_cached_index()
        else:
            index = await ceiling.load_index()
        ceiling.set_index(index)
        ceiling.print_spot_table(index)
    except Exception:
        log.exception("airspace load failed")
        ceiling.print_spot_table(None)
