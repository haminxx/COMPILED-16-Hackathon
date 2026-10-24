"""Ceiling rules on fixture polygons. No network."""

from shapely.geometry import shape

from app.ceiling import Advisories, AirspaceIndex, evaluate_ceiling
from app.config import OUTSIDE_BOX
from app.state import normalize_phone

UCSD = (32.8801, -117.2340)
MYF = (32.8160, -117.1600)
NORTH_ISLAND = (32.6990, -117.2150)
GLIDERPORT = (32.8897, -117.2503)


def _square(lat: float, lon: float, delta: float = 0.012) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - delta, lat - delta],
            [lon + delta, lat - delta],
            [lon + delta, lat + delta],
            [lon - delta, lat + delta],
            [lon - delta, lat - delta],
        ]],
    }


def _feature(lat: float, lon: float, **props) -> dict:
    return {"type": "Feature", "geometry": _square(lat, lon), "properties": props}


def _index(**layers) -> AirspaceIndex:
    names = ("facility", "class_airspace", "dod", "prohibited", "sua", "stadiums", "nd_tfr")
    full = {name: [] for name in names}
    full.update(layers)
    return AirspaceIndex(full, ready=True, layer_ok={name: True for name in names})


def test_phone_matches_on_last_10_digits():
    assert normalize_phone("+1 (858) 555-0199") == "8585550199"
    assert normalize_phone("8585550199") == normalize_phone("+18585550199")


def test_ucsd_is_class_g_400_without_laanc():
    index = _index(class_airspace=[
        _feature(*UCSD, NAME="SAN DIEGO CLASS B", CLASS="B", LOCAL_TYPE="CLASS_B", LOWER_VAL=1800, LOWER_CODE="MSL"),
        _feature(*UCSD, NAME="SAN DIEGO CLASS E5", CLASS="E", LOCAL_TYPE="CLASS_E5", LOWER_VAL=700, LOWER_CODE="SFC"),
        _feature(*UCSD, NAME="LOS ANGELES CLASS E6", CLASS="E", LOCAL_TYPE="CLASS_E6", LOWER_VAL=1200, LOWER_CODE="SFC"),
        _feature(*UCSD, NAME="SAN DIEGO INTERNATIONAL MODE C", CLASS=None, LOCAL_TYPE="MODE C", LOWER_VAL=0, LOWER_CODE="SFC", TYPE_CODE="MODE-C"),
    ])
    result = evaluate_ceiling(*UCSD, index, Advisories())
    assert result.queried is True
    assert result.max_alt_ft_agl == 400
    assert result.regulatory_ft_agl == 400
    assert result.airspace_class == "G"
    assert result.needs_laanc is False
    assert result.nsufr is False
    assert result.tfr is False
    assert "1800" in result.reason
    assert "informational" in result.reason.lower()
    assert "you can fly up to" not in result.reason.lower()


def test_montgomery_gibbs_is_class_d_do_not_fly():
    index = _index(
        facility=[_feature(
            *MYF,
            CEILING=0,
            APT1_NAME="Montgomery-Gibbs Exec",
            APT1_FAAID="MYF",
            AIRSPACE_1="D",
        )],
        class_airspace=[_feature(
            *MYF,
            NAME="SAN DIEGO MONTGOMERY FIELD CLASS D",
            CLASS="D",
            LOCAL_TYPE="CLASS_D",
            LOWER_VAL=0,
            LOWER_CODE="SFC",
        )],
    )
    result = evaluate_ceiling(*MYF, index, Advisories())
    assert result.max_alt_ft_agl == 0
    assert result.airspace_class == "D"
    assert result.needs_laanc is True
    assert "don't fly" in result.reason.lower()
    assert "0" in result.reason
    assert "laanc" in result.reason.lower()
    assert "you can fly up to" not in result.reason.lower()


def test_laanc_ceiling_is_quoted_as_approval_not_permission():
    index = _index(facility=[_feature(
        *MYF, CEILING=100, APT1_NAME="Montgomery-Gibbs Exec", AIRSPACE_1="D",
    )])
    result = evaluate_ceiling(*MYF, index, Advisories())
    assert result.needs_laanc is True
    assert result.max_alt_ft_agl == 100
    assert "with approval the max is 100 feet" in result.reason
    assert "you can fly up to" not in result.reason.lower()


def test_surface_class_e2_requires_laanc_shelves_do_not():
    e2 = _feature(32.75, -117.20, NAME="SURFACE CLASS E2", CLASS="E", LOCAL_TYPE="CLASS_E2", LOWER_VAL=0, LOWER_CODE="SFC")
    result = evaluate_ceiling(32.75, -117.20, _index(class_airspace=[e2]), Advisories())
    assert result.needs_laanc is True
    assert result.max_alt_ft_agl == 0
    assert result.airspace_class == "E"


def test_nsufr_fixture_is_do_not_fly():
    zone = _feature(
        *NORTH_ISLAND,
        Facility="NAS North Island 5",
        Reason="National Security",
        Floor="Surface",
        Ceiling="400' AGL",
        Base="Naval Base Coronado",
    )
    result = evaluate_ceiling(*NORTH_ISLAND, _index(dod=[zone]), Advisories())
    assert result.max_alt_ft_agl == 0
    assert result.nsufr is True
    assert "don't fly" in result.reason.lower()
    assert "north island" in result.reason.lower()


def test_glider_polygon_lowers_recommended_ceiling_only():
    glider = shape({
        "type": "Polygon",
        "coordinates": [[
            [-117.26, 32.884],
            [-117.242, 32.884],
            [-117.242, 32.897],
            [-117.26, 32.897],
            [-117.26, 32.884],
        ]],
    })
    result = evaluate_ceiling(*GLIDERPORT, _index(), Advisories(glider=glider, glider_ft=200))
    assert result.regulatory_ft_agl == 400
    assert result.max_alt_ft_agl == 200
    assert result.airspace_class == "G"
    assert result.needs_laanc is False
    assert "advisory" in result.reason.lower()
    assert "not an airspace restriction" in result.reason.lower()


def test_land_manager_note_does_not_change_airspace():
    advisories = Advisories(notes=[{
        "lat": 32.6735,
        "lon": -117.2423,
        "radius_m": 700,
        "text": "Land-manager advisory: Cabrillo National Monument does not allow drone takeoff. This is not an airspace restriction.",
    }])
    result = evaluate_ceiling(32.6735, -117.2423, _index(), advisories)
    assert result.max_alt_ft_agl == 400
    assert result.airspace_class == "G"
    assert result.needs_laanc is False
    assert "cabrillo" in result.reason.lower()
    assert "not an airspace restriction" in result.reason.lower()


def test_outside_san_diego_is_a_fixed_sentence():
    result = evaluate_ceiling(37.77, -122.42, _index(), Advisories())
    assert result.reason == OUTSIDE_BOX
    assert result.reason == "Freq demo covers San Diego only."
    assert result.max_alt_ft_agl is None


def test_unqueried_layers_do_not_invent_a_ceiling():
    result = evaluate_ceiling(*UCSD, None, Advisories())
    assert result.queried is False
    assert result.max_alt_ft_agl is None
