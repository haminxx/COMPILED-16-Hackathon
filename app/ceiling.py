"""San Diego ceiling engine.

Polygons are fetched once for the demo envelope, cached as GeoJSON, and
tested locally. LAANC applies only to a facility-map grid hit, surface
Class B/C/D (LOWER_CODE SFC or LOWER_VAL 0), or LOCAL_TYPE CLASS_E2.
Class E3/E4/E5/E6 shelves do not lower the 400 ft Class G default.
A Class B floor above the surface is informational only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from . import config
from .geo import haversine_nm, in_san_diego, nm_to_meters

log = logging.getLogger("freq.ceiling")

ARC = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
SERVICES = {
    "facility": "FAA_UAS_FacilityMap_Data",
    "class_airspace": "Class_Airspace",
    "dod": "DoD_Mar_13",
    "prohibited": "Prohibited_Areas",
    "sua": "Special_Use_Airspace",
    "stadiums": "Stadiums",
    "nd_tfr": "National_Defense_Airspace_TFR_Areas",
}
# Facility map pages at 2000 and sets exceededTransferLimit.
PAGE_SIZE = 2000
REQUIRED = ("facility", "class_airspace")
SHELVES = {"CLASS_E3", "CLASS_E4", "CLASS_E5", "CLASS_E6"}
SURFACE_RANK = {"B": 0, "C": 1, "D": 2, "E": 3}
POINT_BUFFER_DEG = {"stadiums": 1.0 / 60.0}

_index: "AirspaceIndex | None" = None
_advisories: "Advisories | None" = None
_spots: list[dict] = []


@dataclass
class CeilingResult:
    max_alt_ft_agl: int | None
    regulatory_ft_agl: int | None
    reason: str
    airspace_class: str
    needs_laanc: bool
    tfr: bool
    nsufr: bool
    queried: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "max_alt_ft_agl": self.max_alt_ft_agl,
            "regulatory_ft_agl": self.regulatory_ft_agl,
            "reason": self.reason,
            "airspace_class": self.airspace_class,
            "needs_laanc": self.needs_laanc,
            "tfr": self.tfr,
            "nsufr": self.nsufr,
            "queried": self.queried,
            "notes": list(self.notes),
        }


@dataclass
class Advisories:
    glider: object | None = None
    glider_ft: int = config.GLIDER_ADVISORY_FT
    notes: list[dict] = field(default_factory=list)
    demo_tfr: object | None = None
    demo_tfr_enabled: bool = False


class AirspaceIndex:
    def __init__(self, layers: dict[str, list], *, ready: bool, layer_ok: dict[str, bool]):
        self.layers = layers
        self.ready = ready
        self.layer_ok = layer_ok
        self._features: dict[str, list] = {}
        self._geoms: dict[str, list] = {}
        self._trees: dict[str, STRtree | None] = {}
        for key, features in layers.items():
            kept_f = []
            kept_g = []
            for feature in features or []:
                geom = _prepare(feature, key)
                if geom is None:
                    continue
                kept_f.append(feature)
                kept_g.append(geom)
            self._features[key] = kept_f
            self._geoms[key] = kept_g
            self._trees[key] = STRtree(kept_g) if kept_g else None

    def hits(self, key: str, lat: float, lon: float) -> list[dict]:
        tree = self._trees.get(key)
        if tree is None:
            return []
        point = Point(lon, lat)
        try:
            idxs = tree.query(point, predicate="intersects")
        except Exception:
            log.warning("spatial query failed for %s", key)
            return []
        features = self._features.get(key) or []
        return [features[int(i)] for i in idxs]


def current_index() -> AirspaceIndex | None:
    return _index


def set_index(index: AirspaceIndex | None) -> None:
    global _index
    _index = index


def current_advisories() -> Advisories:
    return _advisories or Advisories()


def set_advisories(advisories: Advisories) -> None:
    global _advisories
    _advisories = advisories


def spots() -> list[dict]:
    return list(_spots)


def set_spots(items: list[dict]) -> None:
    global _spots
    _spots = list(items)


def load_local_demo() -> tuple[list[dict], Advisories]:
    spots_path = config.DEMO_DIR / "spots.json"
    items = json.loads(spots_path.read_text()) if spots_path.exists() else []
    glider = _read_geom(config.DEMO_DIR / "glider.json")
    tfr = _read_geom(config.DEMO_DIR / "tfr.json")
    notes = [
        {
            "name": item.get("name"),
            "lat": item["lat"],
            "lon": item["lon"],
            "radius_m": item.get("note_radius_m", 800),
            "text": item["note"],
        }
        for item in items
        if item.get("note")
    ]
    advisories = Advisories(
        glider=glider,
        notes=notes,
        demo_tfr=tfr,
        demo_tfr_enabled=config.DEMO_TFR,
    )
    set_spots(items)
    set_advisories(advisories)
    return items, advisories


def evaluate_ceiling(lat: float, lon: float, index: AirspaceIndex | None, advisories: Advisories | None = None) -> CeilingResult:
    advisories = advisories or Advisories()
    if not in_san_diego(lat, lon):
        return CeilingResult(
            max_alt_ft_agl=None,
            regulatory_ft_agl=None,
            reason=config.OUTSIDE_BOX,
            airspace_class="",
            needs_laanc=False,
            tfr=False,
            nsufr=False,
            queried=True,
        )
    if index is None or not index.ready:
        return CeilingResult(
            max_alt_ft_agl=None,
            regulatory_ft_agl=None,
            reason="Airspace data is not available yet.",
            airspace_class="",
            needs_laanc=False,
            tfr=False,
            nsufr=False,
            queried=False,
        )

    notes: list[str] = []
    missing = [name for name in ("dod", "prohibited", "sua", "stadiums", "nd_tfr") if not index.layer_ok.get(name, False)]
    if missing:
        notes.append("Some airspace layers did not load: " + ", ".join(missing) + ".")

    grids = [_props(f) for f in index.hits("facility", lat, lon)]
    classes = [_props(f) for f in index.hits("class_airspace", lat, lon)]
    dod = [_props(f) for f in index.hits("dod", lat, lon)]
    prohibited = [_props(f) for f in index.hits("prohibited", lat, lon)]
    sua = [_props(f) for f in index.hits("sua", lat, lon)]
    stadiums = [_props(f) for f in index.hits("stadiums", lat, lon)]
    nd = [_props(f) for f in index.hits("nd_tfr", lat, lon)]

    grid = _most_restrictive_grid(grids)
    surface = _surface_hits(classes)
    shelves = _shelf_notes(classes)

    needs_laanc = grid is not None or bool(surface)
    airspace_class = "G"
    if surface:
        airspace_class = sorted(surface, key=lambda item: SURFACE_RANK.get(item[0], 9))[0][0]
    elif grid is not None and grid["klass"]:
        airspace_class = grid["klass"]

    regulatory = config.CLASS_G_FT
    nsufr = bool(dod)
    tfr = bool(nd)
    blocks: list[str] = []

    if dod:
        regulatory = 0
        sample = dod[0]
        facility = sample.get("FACILITY") or sample.get("BASE") or "a national security site"
        floor = sample.get("FLOOR") or "Surface"
        top = sample.get("CEILING") or "400 ft AGL"
        blocks.append(
            f"Don't fly here. {facility}, national security UAS flight restriction, {floor} to {top}."
        )
    if prohibited:
        regulatory = 0
        name = prohibited[0].get("NAME") or "a prohibited area"
        blocks.append(f"Don't fly here. Prohibited area {name}.")
    restricted = [item for item in sua if _sua_blocks(item)]
    if restricted:
        regulatory = 0
        name = restricted[0].get("NAME") or "restricted airspace"
        blocks.append(f"Don't fly here. Restricted area {name}.")
    for item in sua:
        if item not in restricted and item.get("NAME"):
            notes.append(f"Special-use airspace {item['NAME']} overhead (not a hard ceiling).")
    if nd:
        regulatory = 0
        tfr = True
        name = nd[0].get("NAME") or "national defense airspace"
        blocks.append(f"Don't fly here. {name}.")
    if advisories.demo_tfr_enabled and advisories.demo_tfr is not None and advisories.demo_tfr.intersects(Point(lon, lat)):
        regulatory = 0
        tfr = True
        blocks.append("Don't fly here. Demo TFR is active over this spot.")

    if grid is not None:
        regulatory = min(regulatory, grid["ceiling"])
        airport = grid["name"]
        klass = grid["klass"] or airspace_class or "controlled"
        if grid["ceiling"] <= 0:
            blocks.append(
                f"Don't fly here. This is {airport} Class {klass}. "
                "You need LAANC approval; the facility-map ceiling is 0 feet."
            )
        else:
            blocks.append(
                f"This is {airport} Class {klass}. You need LAANC approval; "
                f"with approval the max is {grid['ceiling']} feet."
            )
    elif surface:
        regulatory = 0
        klass, name = surface[0][0], surface[0][1]
        blocks.append(
            f"Don't fly here. This is {name} Class {klass}. "
            "You need LAANC approval before flying at any altitude."
        )

    for hit in stadiums:
        label = hit.get("NAME") or "a stadium"
        notes.append(
            f"Near {label}. A game-day stadium TFR can apply; this layer is not an active TFR by itself."
        )

    for note in _nearby_notes(lat, lon, advisories.notes):
        notes.append(note)

    glider = False
    if advisories.glider is not None and advisories.glider.intersects(Point(lon, lat)):
        glider = True

    recommended = regulatory
    if glider:
        recommended = min(regulatory, advisories.glider_ft)

    if not blocks:
        lead = f"Class G, {regulatory} feet."
        if shelves:
            lead += " " + " ".join(shelves[:2])
        reason_parts = [lead]
    else:
        reason_parts = blocks[:2]
        if shelves and airspace_class == "G":
            reason_parts.append(" ".join(shelves[:2]))

    if glider and recommended < regulatory:
        reason_parts.append(
            f"Advisory: Torrey Pines glider activity lowers the recommended ceiling to "
            f"{advisories.glider_ft} feet. Gliders often do not broadcast ADS-B. "
            "This is not an airspace restriction."
        )
    elif glider:
        reason_parts.append(
            "Advisory: Torrey Pines glider activity. Gliders often do not broadcast ADS-B. "
            "This is not an airspace restriction."
        )

    # Land-manager notes are not airspace and do not change the number.
    spoken_notes = [n for n in notes if "did not load" not in n][:2]
    reason_parts.extend(spoken_notes)
    if any("did not load" in n for n in notes):
        reason_parts.append(next(n for n in notes if "did not load" in n))

    reason = " ".join(part.strip() for part in reason_parts if part.strip())
    return CeilingResult(
        max_alt_ft_agl=int(recommended),
        regulatory_ft_agl=int(regulatory),
        reason=reason,
        airspace_class=airspace_class,
        needs_laanc=needs_laanc,
        tfr=tfr,
        nsufr=nsufr,
        queried=True,
        notes=notes,
    )


def ceiling_here(lat: float, lon: float) -> CeilingResult:
    return evaluate_ceiling(lat, lon, current_index(), current_advisories())


def print_spot_table(index: AirspaceIndex | None = None) -> None:
    index = current_index() if index is None else index
    items = spots()
    advisories = current_advisories()
    print("Freq spot ceilings", flush=True)
    if index is None or not index.ready:
        print("  status: unavailable (facility map and class airspace were not queried)", flush=True)
        for item in items:
            print(f"  {item.get('name')}: unavailable", flush=True)
        return
    counts = {key: len(index.layers.get(key) or []) for key in SERVICES}
    print(
        "  queried polygons: "
        + ", ".join(f"{key}={counts[key]}" for key in SERVICES),
        flush=True,
    )
    print("  rec = recommended after advisories; faa = queried regulatory limit", flush=True)
    print(f"  {'spot':<32} {'rec':>5} {'faa':>5} {'cls':<4} {'laanc':<5} {'nsufr':<5} reason", flush=True)
    for item in items:
        result = evaluate_ceiling(float(item["lat"]), float(item["lon"]), index, advisories)
        if not result.queried or result.max_alt_ft_agl is None:
            print(f"  {item['name']:<32} unavailable", flush=True)
            continue
        print(
            f"  {item['name']:<32} {result.max_alt_ft_agl:5d} {result.regulatory_ft_agl:5d} "
            f"{(result.airspace_class or '-'): <4} "
            f"{'yes' if result.needs_laanc else 'no':<5} "
            f"{'yes' if result.nsufr else 'no':<5} {result.reason}",
            flush=True,
        )


async def load_index() -> AirspaceIndex:
    """Fetch the San Diego envelope. Each layer fails soft and may reuse cache."""
    from .sources import get_json

    layers: dict[str, list] = {}
    layer_ok: dict[str, bool] = {}
    for key, service in SERVICES.items():
        cached = _read_cache(key)
        features = await _fetch_service(service, get_json)
        if features is None:
            if cached is not None:
                log.warning("%s fetch failed; using cache (%s features)", key, len(cached))
                layers[key] = cached
                layer_ok[key] = True
            else:
                log.warning("%s unavailable", key)
                layers[key] = []
                layer_ok[key] = False
            continue
        _write_cache(key, features)
        layers[key] = features
        layer_ok[key] = True
        log.info("loaded %s (%s features)", key, len(features))
    ready = all(layer_ok.get(key) for key in REQUIRED)
    return AirspaceIndex(layers, ready=ready, layer_ok=layer_ok)


def load_cached_index() -> AirspaceIndex:
    layers = {}
    layer_ok = {}
    for key in SERVICES:
        cached = _read_cache(key)
        if cached is None:
            layers[key] = []
            layer_ok[key] = False
        else:
            layers[key] = cached
            layer_ok[key] = True
    ready = all(layer_ok.get(key) for key in REQUIRED)
    return AirspaceIndex(layers, ready=ready, layer_ok=layer_ok)


async def _fetch_service(service: str, get_json) -> list | None:
    url = f"{ARC}/{service}/FeatureServer/0/query"
    offset = 0
    features: list = []
    while offset <= PAGE_SIZE * 20:
        params = {
            "geometry": config.ENVELOPE,
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
            "resultRecordCount": str(PAGE_SIZE),
            "resultOffset": str(offset),
        }
        data = await get_json(url, params=params, timeout=45.0)
        if not isinstance(data, dict) or data.get("error") or data.get("type") != "FeatureCollection":
            return None if offset == 0 else features
        page = data.get("features") or []
        features.extend(page)
        exceeded = data.get("exceededTransferLimit")
        if exceeded is None:
            exceeded = (data.get("properties") or {}).get("exceededTransferLimit")
        if not page or exceeded is False or len(page) < PAGE_SIZE:
            return features
        offset += len(page)
    return features


def _read_cache(key: str) -> list | None:
    path = config.CACHE_DIR / f"{key}.geojson"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        return None
    return features


def _write_cache(key: str, features: list) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CACHE_DIR / f"{key}.geojson"
    tmp = path.with_suffix(".geojson.tmp")
    payload = {"type": "FeatureCollection", "features": features}
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _read_geom(path: Path):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("type") == "FeatureCollection":
        geoms = []
        for feature in data.get("features") or []:
            geom = feature.get("geometry")
            if geom:
                geoms.append(shape(geom))
        if not geoms:
            return None
        from shapely.ops import unary_union
        return unary_union(geoms)
    if data.get("type") == "Feature":
        return shape(data["geometry"])
    if data.get("type") in {"Polygon", "MultiPolygon"}:
        return shape(data)
    return None


def _prepare(feature: dict, layer: str):
    geom = feature.get("geometry")
    if not geom:
        return None
    try:
        polygon = shape(geom)
    except Exception:
        return None
    if polygon.is_empty:
        return None
    try:
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except Exception:
        return None
    if polygon.geom_type == "Point":
        polygon = polygon.buffer(POINT_BUFFER_DEG.get(layer, 0.002))
    return polygon


def _props(feature: dict) -> dict:
    raw = feature.get("properties") or feature.get("attributes") or {}
    return {str(key).upper(): value for key, value in raw.items()}


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _class_letter(value) -> str:
    text = str(value or "").upper().replace("CLASS", "").strip()
    if text in {"A", "B", "C", "D", "E", "G"}:
        return text
    return ""


def _local_type(props: dict) -> str:
    return str(props.get("LOCAL_TYPE") or "").upper().replace(" ", "_")


def _is_surface(props: dict) -> str | None:
    local = _local_type(props)
    klass = _class_letter(props.get("CLASS"))
    code = str(props.get("LOWER_CODE") or "").upper()
    val = _num(props.get("LOWER_VAL"))
    if local == "CLASS_E2":
        return "E"
    if klass in {"B", "C", "D"} and (code == "SFC" or val == 0):
        return klass
    return None


def _surface_hits(features: list[dict]) -> list[tuple[str, str]]:
    found = []
    for props in features:
        klass = _is_surface(props)
        if not klass:
            continue
        name = props.get("NAME") or f"Class {klass}"
        found.append((klass, str(name)))
    found.sort(key=lambda item: SURFACE_RANK.get(item[0], 9))
    return found


def _shelf_notes(features: list[dict]) -> list[str]:
    notes = []
    seen = set()
    for props in features:
        if _is_surface(props):
            continue
        local = _local_type(props)
        klass = _class_letter(props.get("CLASS"))
        informational = local in SHELVES or klass in {"B", "C", "D", "E"}
        if not informational or klass == "A":
            continue
        if local.startswith("MODE") or str(props.get("TYPE_CODE") or "").upper().startswith("MODE"):
            continue
        val = _num(props.get("LOWER_VAL"))
        if val is None:
            continue
        code = str(props.get("LOWER_CODE") or "").upper() or "MSL"
        # E3–E6 use LOWER_CODE SFC with a non-zero floor. Say AGL so it is
        # not mistaken for airspace that starts at the surface.
        height = f"{_fmt_num(val)} ft AGL" if local in SHELVES else f"{_fmt_num(val)} ft {code}"
        name = str(props.get("NAME") or f"Class {klass}")
        label = f"{name} floor {height} overhead (informational only)."
        if label not in seen:
            seen.add(label)
            notes.append(label)
    return notes[:3]


def _most_restrictive_grid(features: list[dict]) -> dict | None:
    best = None
    for props in features:
        ceiling = _num(props.get("CEILING"))
        if ceiling is None:
            continue
        ceiling_i = int(ceiling)
        name = str(props.get("APT1_NAME") or props.get("NAME") or "this airport")
        klass = _class_letter(props.get("AIRSPACE_1"))
        item = {"ceiling": ceiling_i, "name": name, "klass": klass}
        if best is None or item["ceiling"] < best["ceiling"]:
            best = item
    return best


def _sua_blocks(props: dict) -> bool:
    code = str(props.get("TYPE_CODE") or "").upper()
    name = str(props.get("NAME") or "").upper()
    if code in {"P", "R"}:
        return True
    if "PROHIB" in name or name.startswith("R-") or name.startswith("P-"):
        return True
    return False


def _nearby_notes(lat: float, lon: float, notes: list[dict]) -> list[str]:
    found = []
    for note in notes:
        try:
            distance_m = nm_to_meters(haversine_nm(lat, lon, float(note["lat"]), float(note["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
        if distance_m <= float(note.get("radius_m") or 800):
            found.append(str(note["text"]))
    return found


def _fmt_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)
