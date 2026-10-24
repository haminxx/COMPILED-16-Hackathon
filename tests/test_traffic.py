"""Traffic tiers on a synthetic helicopter. No network."""

from app.geo import format_wind
from app.traffic import blank_memory, choose, commit, prepare

TAIL = "N123HX"


def _heli(**overrides):
    aircraft = {
        "id": "abc123",
        "category": "A7",
        "phrase": "A helicopter",
        "distance_nm": 4.2,
        "bearing": "south",
        "agl_ft": 700,
        "spoken_alt_ft": 700,
        "gs": 90,
        "track": 0,
        "north_nm": -4.2,
        "east_nm": 0.0,
        "flight": TAIL,
        "hex": "abc123",
    }
    aircraft.update(overrides)
    return aircraft


def _spoken(injection) -> str:
    assert injection is not None
    assert TAIL not in injection.content
    assert "abc123" not in injection.content
    return injection.content


def test_helicopter_drips_then_alerts_and_respects_cooldown():
    memory = blank_memory()
    far = _heli()
    first = choose([far], memory, None, now=0)
    assert first is not None and first.tier == "drip"
    text = _spoken(first)
    assert "helicopter" in text.lower()
    assert "south" in text
    commit(memory, first, 0)

    held = choose([_heli()], memory, None, now=10)
    assert held is None

    close = _heli(distance_nm=2.0, north_nm=-2.0, spoken_alt_ft=700, agl_ft=700)
    alert = choose([close], memory, None, now=20)
    assert alert is not None and alert.tier == "alert"
    assert alert.interrupt is True
    spoken = _spoken(alert)
    assert "700" in spoken
    assert "Descend to 100 feet" in spoken
    assert "closing" in spoken
    commit(memory, alert, 20)

    # Past the 8 second gap, still inside the 45 second alert cooldown.
    assert choose([close], memory, None, now=30) is None


def test_closing_uses_cpa_not_a_decreasing_range_miss():
    memory = blank_memory()
    # East of the pilot, flying north: range can fall while CPA stays about 2 NM.
    offset = _heli(
        distance_nm=2.236,
        north_nm=-1.0,
        east_nm=2.0,
        gs=120,
        track=0,
        bearing="south-east",
        agl_ft=700,
    )
    memory["ranges"]["abc123"] = 3.0
    injection = choose([offset], memory, None, now=0)
    assert injection is None or injection.tier == "drip"
    assert injection is None or injection.tier != "alert"


def test_range_fallback_when_track_is_missing():
    memory = blank_memory()
    first = _heli(track=None, gs=None, distance_nm=2.0, north_nm=-2.0, east_nm=0.0)
    drip = choose([first], memory, None, now=0)
    assert drip is not None and drip.tier == "drip"
    commit(memory, drip, 0)
    closer = _heli(track=None, gs=None, distance_nm=1.2, north_nm=-1.2, east_nm=0.0)
    alert = choose([closer], memory, None, now=20)
    assert alert is not None and alert.tier == "alert"


def test_airliner_categories_above_1000_ft_skip_the_drip_tier():
    memory = blank_memory()
    for category in ("A3", "A4", "A5"):
        high = _heli(id=category, category=category, phrase="A large aircraft", agl_ft=2000, spoken_alt_ft=2000)
        assert choose([high], blank_memory(), None, now=0) is None
    helicopter = _heli(agl_ft=2000, spoken_alt_ft=2000)
    kept = choose([helicopter], memory, None, now=0)
    assert kept is not None and kept.tier == "drip"
    low_airliner = _heli(id="low", category="A3", phrase="A large aircraft", agl_ft=800, spoken_alt_ft=800)
    allowed = choose([low_airliner], blank_memory(), None, now=0)
    assert allowed is not None and allowed.tier == "drip"


def test_light_aircraft_still_drip():
    for category, phrase in (("A1", "A light aircraft"), ("A2", "A small aircraft")):
        aircraft = _heli(id=category, category=category, phrase=phrase)
        injection = choose([aircraft], blank_memory(), None, now=0)
        assert injection is not None and injection.tier == "drip"
        assert phrase.split()[-1] in injection.content
        assert TAIL not in injection.content


def test_prepare_skips_ground_and_stale_positions():
    pilot = (32.88, -117.23)
    rows = prepare(
        [
            {"hex": "aaa", "lat": 32.87, "lon": -117.23, "alt_baro": "ground", "category": "A7", "seen_pos": 1, "flight": TAIL},
            {"hex": "bbb", "lat": 32.87, "lon": -117.23, "alt_baro": 1200, "category": "A7", "seen_pos": 16, "flight": TAIL},
            {"hex": "ccc", "lat": 32.86, "lon": -117.23, "alt_baro": 900, "category": "A7", "seen_pos": 2, "gs": 80, "track": 0, "flight": TAIL},
        ],
        pilot[0],
        pilot[1],
        ground_ft=200,
    )
    assert [row["id"] for row in rows] == ["ccc"]
    assert rows[0]["agl_ft"] == 700
    assert rows[0]["spoken_alt_ft"] == 700
    assert TAIL not in rows[0]["phrase"]


def test_wind_converts_kmh_and_allows_null_gust():
    assert format_wind(14.816, None, 270) == "Wind west 8 knots."
    assert "gust" not in format_wind(14.816, None, 270)
    gusting = format_wind(14.816, 22.224, 270)
    assert gusting.startswith("Wind west 8 knots, gusts ")
    assert format_wind(None, None, None) == "Wind unavailable."
