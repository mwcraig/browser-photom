"""validate_metadata(): the dashboard's only user-entered input.

bandaid's Seestar50 profile maps site_lat/site_lon/site_elev/observer out of
the FITS header, and USER_META is applied last as an override. Real Seestar
frames carry SITELAT and SITELONG but no SITEELEV and no observer code, so
those two are required here and lat/lon are optional overrides that fall back
to the header when left blank.
"""

import pytest

from photom_dashboard import validate_metadata


def test_minimal_valid_input():
    meta, errors = validate_metadata(observer="lgeb", site_elev=1675)
    assert errors == []
    assert meta == {"observer": "LGEB", "site_elev": 1675.0}


def test_observer_is_trimmed_and_uppercased():
    meta, errors = validate_metadata(observer="  lgeb  ", site_elev="1675")
    assert errors == []
    assert meta["observer"] == "LGEB"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_observer_required(blank):
    meta, errors = validate_metadata(observer=blank, site_elev=1675)
    assert "observer" not in meta
    assert any("observer" in e.lower() for e in errors)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_elevation_required(blank):
    meta, errors = validate_metadata(observer="LGEB", site_elev=blank)
    assert "site_elev" not in meta
    assert any("elevation" in e.lower() for e in errors)


def test_elevation_must_be_numeric():
    meta, errors = validate_metadata(observer="LGEB", site_elev="high up")
    assert "site_elev" not in meta
    assert any("number" in e.lower() for e in errors)


def test_negative_elevation_is_allowed():
    # Below sea level is a real place to observe from (Dead Sea, Death Valley).
    meta, errors = validate_metadata(observer="LGEB", site_elev=-50)
    assert errors == []
    assert meta["site_elev"] == -50.0


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_lat_lon_are_omitted_so_the_header_supplies_them(blank):
    meta, errors = validate_metadata(
        observer="LGEB", site_elev=1675, site_lat=blank, site_lon=blank
    )
    assert errors == []
    assert "site_lat" not in meta
    assert "site_lon" not in meta


def test_supplied_lat_lon_override():
    meta, errors = validate_metadata(
        observer="LGEB", site_elev=1675, site_lat="30.5952", site_lon="-103.936"
    )
    assert errors == []
    assert meta["site_lat"] == pytest.approx(30.5952)
    assert meta["site_lon"] == pytest.approx(-103.936)


@pytest.mark.parametrize("lat", [90.1, -90.1, 1000])
def test_latitude_range_checked(lat):
    meta, errors = validate_metadata(observer="LGEB", site_elev=1675, site_lat=lat)
    assert "site_lat" not in meta
    assert any("latitude" in e.lower() for e in errors)


@pytest.mark.parametrize("lon", [180.1, -180.1, 1000])
def test_longitude_range_checked(lon):
    meta, errors = validate_metadata(observer="LGEB", site_elev=1675, site_lon=lon)
    assert "site_lon" not in meta
    assert any("longitude" in e.lower() for e in errors)


@pytest.mark.parametrize("edge", [90, -90])
def test_latitude_bounds_are_inclusive(edge):
    meta, errors = validate_metadata(observer="LGEB", site_elev=1675, site_lat=edge)
    assert errors == []
    assert meta["site_lat"] == float(edge)


def test_non_numeric_lat_lon_reported():
    meta, errors = validate_metadata(
        observer="LGEB", site_elev=1675, site_lat="north", site_lon="west"
    )
    assert "site_lat" not in meta
    assert "site_lon" not in meta
    assert len(errors) == 2


def test_all_errors_are_collected_not_just_the_first():
    meta, errors = validate_metadata(
        observer="", site_elev="", site_lat=95, site_lon=-500
    )
    assert meta == {}
    assert len(errors) == 4
