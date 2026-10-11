"""Finds businesses near a campus.

Primary source: Google Places API (New) when ``GOOGLE_MAPS_API_KEY`` is set. Fallback (and default):
OpenStreetMap via the free Overpass API, with a mirror if the main server is busy. The result always
says which source was used and why a fallback happened, so the Discovery agent can reason about it.
"""

from __future__ import annotations

import hashlib
import math
import re

import httpx

from app.graph.state import Brand
from app.tools.base import ToolError

# Our category -> Google place types and OpenStreetMap tags.
CATEGORIES: dict[str, dict] = {
    "cafe": {"google": ["cafe", "coffee_shop"], "osm": [("amenity", "cafe")]},
    "restaurant": {"google": ["restaurant", "fast_food_restaurant"],
                   "osm": [("amenity", "restaurant"), ("amenity", "fast_food")]},
    "gym": {"google": ["gym", "fitness_center"], "osm": [("leisure", "fitness_centre"), ("leisure", "sports_centre")]},
    "salon": {"google": ["beauty_salon", "hair_salon", "hair_care"], "osm": [("shop", "hairdresser"), ("shop", "beauty")]},
    "coaching": {"google_text": "coaching centre",
                 "osm": [("amenity", "prep_school"), ("amenity", "training"), ("amenity", "language_school")]},
    "bookstore": {"google": ["book_store"], "osm": [("shop", "books")]},
    "stationery": {"google_text": "stationery shop", "osm": [("shop", "stationery")]},
    "clothing": {"google": ["clothing_store"], "osm": [("shop", "clothes")]},
    "electronics": {"google": ["electronics_store", "cell_phone_store"],
                    "osm": [("shop", "electronics"), ("shop", "mobile_phone")]},
    "bakery": {"google": ["bakery"], "osm": [("shop", "bakery")]},
}

OVERPASS_MIRRORS = ["https://overpass.kumi.systems/api/interpreter"]
GOOGLE_FIELDS = "places.id,places.displayName,places.formattedAddress,places.location,places.websiteUri,places.types"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def _id(source: str, raw_id: str) -> str:
    return f"{source}-{hashlib.sha1(raw_id.encode()).hexdigest()[:10]}"


class PlacesTool:
    def __init__(self, google_api_key: str = "", overpass_url: str = "https://overpass-api.de/api/interpreter",
                 timeout: float = 25.0) -> None:
        self.google_api_key = google_api_key
        self.overpass_urls = [overpass_url, *[m for m in OVERPASS_MIRRORS if m != overpass_url]]
        self.timeout = timeout

    async def search(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> dict:
        unknown = [c for c in categories if c not in CATEGORIES]
        if unknown or not categories:
            raise ToolError(f"unknown categories {unknown}; use any of {sorted(CATEGORIES)}")
        fallback_reason = None
        if self.google_api_key:
            try:
                places = await self._google(lat, lng, radius_km, categories)
                if places:
                    return {"source": "google_places", "fallback_reason": None, "places": places}
                fallback_reason = "Google Places returned no results"
            except Exception as exc:
                fallback_reason = f"Google Places failed: {type(exc).__name__}: {exc}"
        else:
            fallback_reason = "GOOGLE_MAPS_API_KEY not set"
        places = await self._overpass(lat, lng, radius_km, categories)
        return {"source": "openstreetmap", "fallback_reason": fallback_reason, "places": places}

    async def find_contact(self, website: str) -> str | None:
        return await find_contact_email(website)

    async def _google(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> list[Brand]:
        headers = {"X-Goog-Api-Key": self.google_api_key, "X-Goog-FieldMask": GOOGLE_FIELDS}
        circle = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_km * 1000}}
        results: list[Brand] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for category in categories:
                spec = CATEGORIES[category]
                if spec.get("google"):
                    body = {"includedTypes": spec["google"], "maxResultCount": 20, "locationRestriction": circle}
                    url = "https://places.googleapis.com/v1/places:searchNearby"
                else:
                    body = {"textQuery": spec["google_text"], "maxResultCount": 20, "locationBias": circle}
                    url = "https://places.googleapis.com/v1/places:searchText"
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                for p in resp.json().get("places", []):
                    loc = p.get("location", {})
                    plat, plng = loc.get("latitude"), loc.get("longitude")
                    if plat is None or plng is None:
                        continue
                    distance = haversine_km(lat, lng, plat, plng)
                    if distance > radius_km:
                        continue
                    results.append(Brand(
                        id=_id("g", p["id"]), name=p.get("displayName", {}).get("text", "Unnamed"),
                        category=category, address=p.get("formattedAddress", ""), lat=plat, lng=plng,
                        distance_km=distance, website=p.get("websiteUri"),
                    ))
        return results

    def overpass_query(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> str:
        radius_m = int(radius_km * 1000)
        clauses = [
            f'nwr["{key}"="{value}"]["name"](around:{radius_m},{lat},{lng});'
            for category in categories for key, value in CATEGORIES[category]["osm"]
        ]
        return f"[out:json][timeout:25];({''.join(clauses)});out center tags 120;"

    async def _overpass(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> list[Brand]:
        query = self.overpass_query(lat, lng, radius_km, categories)
        errors = []
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": "Paytriq/0.1 (student project)"}) as client:
            for url in self.overpass_urls:
                try:
                    resp = await client.post(url, data={"data": query})
                    resp.raise_for_status()
                    return self._parse_osm(resp.json(), lat, lng, categories)
                except Exception as exc:
                    errors.append(f"{url}: {type(exc).__name__} {exc}")
        raise ToolError("OpenStreetMap Overpass unavailable: " + " | ".join(errors))

    @staticmethod
    def _parse_osm(data: dict, lat: float, lng: float, categories: list[str]) -> list[Brand]:
        tag_to_category = {tag: c for c in categories for tag in CATEGORIES[c]["osm"]}
        brands: list[Brand] = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            plat = el.get("lat") or el.get("center", {}).get("lat")
            plng = el.get("lon") or el.get("center", {}).get("lon")
            category = next((c for (k, v), c in tag_to_category.items() if tags.get(k) == v), None)
            if not tags.get("name") or plat is None or plng is None or category is None:
                continue
            address = tags.get("addr:full") or ", ".join(
                tags[k] for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city") if tags.get(k)
            )
            brands.append(Brand(
                id=_id("osm", f"{el.get('type')}/{el.get('id')}"), name=tags["name"], category=category,
                address=address or "Address not listed", lat=plat, lng=plng,
                distance_km=haversine_km(lat, lng, plat, plng),
                contact_email=tags.get("email") or tags.get("contact:email"),
                website=tags.get("website") or tags.get("contact:website"),
            ))
        return brands


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "example.com", "sentry.io", "wixpress.com")


def extract_emails(html: str) -> list[str]:
    found = []
    for email in EMAIL_RE.findall(html):
        email = email.lower().strip(".")
        if not email.endswith(IGNORED_EMAIL_SUFFIXES) and email not in found:
            found.append(email)
    return found


async def find_contact_email(website: str, timeout: float = 8.0) -> str | None:
    """Looks for a public contact email on the business website (home page, then /contact)."""
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "Paytriq/0.1 (student project)"}) as client:
        for url in (website, website.rstrip("/") + "/contact"):
            try:
                resp = await client.get(url)
            except httpx.HTTPError:
                continue
            emails = extract_emails(resp.text[:300_000])
            if emails:
                return emails[0]
    return None
