"""Places tool parsing and Google -> OpenStreetMap fallback (no network)."""

import pytest

from app.tools.base import ToolError
from app.tools.places import PlacesTool, extract_emails, haversine_km

OSM = {"elements": [
    {"type": "node", "id": 1, "lat": 18.521, "lon": 73.857,
     "tags": {"amenity": "cafe", "name": "Cafe Goodluck", "email": "hi@goodluck.in", "addr:street": "FC Road"}},
    {"type": "way", "id": 2, "center": {"lat": 18.53, "lon": 73.86},
     "tags": {"leisure": "fitness_centre", "name": "Iron Gym", "website": "irongym.in"}},
    {"type": "node", "id": 3, "lat": 18.52, "lon": 73.85, "tags": {"amenity": "cafe"}},  # no name: skipped
]}


def test_parse_osm_maps_categories_and_contacts():
    brands = PlacesTool._parse_osm(OSM, 18.5204, 73.8567, ["cafe", "gym"])
    assert [(b.name, b.category) for b in brands] == [("Cafe Goodluck", "cafe"), ("Iron Gym", "gym")]
    assert brands[0].contact_email == "hi@goodluck.in" and brands[0].address == "FC Road"
    assert brands[1].website == "irongym.in"
    assert brands[0].distance_km < 0.2


def test_overpass_query_uses_radius_in_metres_and_one_clause_per_key():
    q = PlacesTool().overpass_query(18.5, 73.8, 2.5, ["salon", "bakery", "cafe"])
    assert "(around:2500,18.5,73.8)" in q
    assert '"shop"~"^(bakery|beauty|hairdresser)$"' in q
    assert q.count("nwr[") == 2  # amenity + shop


async def test_busy_overpass_falls_back_to_nominatim(monkeypatch):
    tool = PlacesTool()

    async def overpass_down(*a):
        raise ToolError("OpenStreetMap Overpass unavailable: 504 Gateway Timeout")

    async def nominatim(*a):
        return PlacesTool._parse_nominatim(NOMINATIM, 18.5204, 73.8567, "cafe", 2)

    monkeypatch.setattr(tool, "_overpass", overpass_down)
    monkeypatch.setattr(tool, "_nominatim", nominatim)
    result = await tool.search(18.5204, 73.8567, 2, ["cafe"])
    assert result["source"] == "openstreetmap_nominatim"
    assert "504" in result["fallback_reason"]
    assert [b.name for b in result["places"]] == ["Cafe Goodluck"]


NOMINATIM = [
    {"osm_type": "node", "osm_id": 1, "lat": "18.521", "lon": "73.857", "name": "Cafe Goodluck",
     "display_name": "Cafe Goodluck, FC Road, Deccan, Pune", "extratags": {"email": "hi@goodluck.in"}},
    {"osm_type": "node", "osm_id": 2, "lat": "18.70", "lon": "73.99", "name": "Far Away Cafe",
     "display_name": "Far Away Cafe, Somewhere"},  # outside the radius: dropped
]


def test_parse_nominatim_keeps_nearby_places_with_contacts():
    [b] = PlacesTool._parse_nominatim(NOMINATIM, 18.5204, 73.8567, "cafe", 2)
    assert (b.name, b.category, b.contact_email, b.address) == ("Cafe Goodluck", "cafe", "hi@goodluck.in",
                                                                 "FC Road, Deccan, Pune")


async def test_unknown_category_is_rejected():
    with pytest.raises(ToolError):
        await PlacesTool().search(18.5, 73.8, 2, ["casino"])


async def test_google_failure_falls_back_to_openstreetmap(monkeypatch):
    tool = PlacesTool(google_api_key="key")

    async def google_down(*a):
        raise RuntimeError("403 billing disabled")

    async def osm(*a):
        return PlacesTool._parse_osm(OSM, 18.52, 73.85, ["cafe"])

    monkeypatch.setattr(tool, "_google", google_down)
    monkeypatch.setattr(tool, "_overpass", osm)
    result = await tool.search(18.52, 73.85, 2, ["cafe"])
    assert result["source"] == "openstreetmap"
    assert "billing disabled" in result["fallback_reason"]


def test_email_extraction_skips_asset_names():
    html = '<a href="mailto:Owner@Cafe.in">x</a> logo@2x.png owner@cafe.in contact@shop.co.in'
    assert extract_emails(html) == ["owner@cafe.in", "contact@shop.co.in"]


def test_haversine():
    assert haversine_km(18.52, 73.85, 18.52, 73.85) == 0
    assert 1.0 < haversine_km(18.52, 73.85, 18.53, 73.86) < 1.6
