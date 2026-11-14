"""Google Maps search: grid math, the Places client against a fake Google, and the find endpoint.
No network, no cost."""

import asyncio
import functools
import json
import math

import httpx
import pytest

from dependencies import get_optional_places
from main import app
from services.google_places import (
    MAX_DEPTH,
    PlacesClient,
    PlacesError,
    _distance_m,
    initial_grid,
    shared_domain,
    split,
    split_box,
)
from tests.conftest import ICP_ID
from tests.test_pipeline_api import WS, env  # noqa: F401  (the pipeline fixture, reused)


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---------- Grid ----------


def test_grid_sizes_match_lead_intel() -> None:
    # Up to ~15 miles is one circle; beyond that a grid of 15-mile circles.
    assert len(initial_grid(41.88, -87.63, 10)) == 1
    assert len(initial_grid(41.88, -87.63, 25)) == 9
    big = initial_grid(41.88, -87.63, 50)
    assert len(big) == 29 and all(c.radius_m <= 50_000 for c in big)
    # A full circle splits into 9 smaller ones, and stops splitting at the depth cap.
    kids = split(big[0])
    assert len(kids) == 9 and all(k.depth == 1 and k.radius_m < big[0].radius_m for k in kids)
    deepest = big[0]
    for _ in range(MAX_DEPTH):
        deepest = split(deepest)[0]
    assert deepest.depth == MAX_DEPTH and split(deepest) == []


def test_grid_leaves_no_gaps() -> None:
    lat, lng, miles = 41.88, -87.63, 25
    cells = initial_grid(lat, lng, miles)
    radius = miles * 1609.344
    # Every point inside the search area, on rings at 8 bearings, is inside some circle.
    for frac in (0, 0.3, 0.6, 0.95):
        for bearing in range(0, 360, 45):
            d = radius * frac
            b = math.radians(bearing)
            plat = lat + math.degrees(d * math.cos(b) / 6_371_000)
            plng = lng + math.degrees(d * math.sin(b) / (6_371_000 * math.cos(math.radians(lat))))
            assert any(_distance_m(c.lat, c.lng, plat, plng) <= c.radius_m for c in cells), (frac, bearing)


def test_shared_domains() -> None:
    assert shared_domain("heavenly.square.site") == "platform"
    assert shared_domain("facebook.com") == "platform"
    assert shared_domain("local.targetoptical.com") == "chain"
    assert shared_domain("fedex.com") == "chain"
    assert shared_domain("upmerch.com") is None and shared_domain(None) is None


# ---------- Client against a fake Google ----------


def place(i: int, **kw) -> dict:
    return {
        "id": f"p{i}", "displayName": {"text": f"Shop {i}"}, "formattedAddress": f"{i} Main St, Chicago, IL",
        "addressComponents": [
            {"longText": "Chicago", "types": ["locality"]}, {"shortText": "IL", "types": ["administrative_area_level_1"]},
            {"shortText": "US", "types": ["country"]},
        ],
        "websiteUri": f"https://shop{i}.com/", "nationalPhoneNumber": "(312) 555-0100", "rating": 4.5,
        "userRatingCount": 30, "businessStatus": "OPERATIONAL", **kw,
    }  # fmt: skip


CHICAGO = {
    "places": [{
        "displayName": {"text": "Chicago"}, "formattedAddress": "Chicago, IL, USA", "location": {"latitude": 41.88, "longitude": -87.63},
        "types": ["locality", "political"], "addressComponents": [{"shortText": "US", "types": ["country"]}],
    }]
}  # fmt: skip


class FakeGoogle:
    """Answers centre lookups and searches. `pages` is what each search request returns, in order."""

    def __init__(self, center=None, pages=None, status=200) -> None:
        self.center, self.pages, self.status = center or CHICAGO, list(pages or []), status
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"body": body, "mask": request.headers["X-Goog-FieldMask"], "key": request.headers["X-Goog-Api-Key"]})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "API key not valid."}})
        if "locationBias" not in body and "locationRestriction" not in body:
            return httpx.Response(200, json=self.center)
        return httpx.Response(200, json=self.pages.pop(0) if self.pages else {"places": []})


def client_for(google: FakeGoogle) -> PlacesClient:
    return PlacesClient("test-key", transport=httpx.MockTransport(google))


@sync
async def test_center_resolves_and_guards() -> None:
    google = FakeGoogle()
    places = client_for(google)
    lat, lng, label = await places.center("Chicago, IL")
    assert (lat, lng, label) == (41.88, -87.63, "Chicago, IL, USA")
    req = google.requests[0]
    assert req["key"] == "test-key" and "places.websiteUri" not in req["mask"] and req["body"]["regionCode"] == "US"

    karachi = {"places": [{"displayName": {"text": "Springfield School"}, "location": {"latitude": 24.8, "longitude": 67.0},
                           "types": ["school", "establishment"], "addressComponents": [{"shortText": "PK", "types": ["country"]}]}]}  # fmt: skip
    with pytest.raises(PlacesError, match="business"):
        await client_for(FakeGoogle(center=karachi)).center("Springfield")
    # Any country: Karachi itself is searched, not refused (2026-11-14).
    city = {"places": [{"displayName": {"text": "Karachi"}, "formattedAddress": "Karachi, Pakistan", "location": {"latitude": 24.86, "longitude": 67.0},
                        "types": ["locality", "political"], "addressComponents": [{"shortText": "PK", "types": ["country"]}]}]}  # fmt: skip
    assert await client_for(FakeGoogle(center=city)).center("Karachi") == (24.86, 67.0, "Karachi, Pakistan")
    shop = {"places": [{"displayName": {"text": "Ink Co"}, "location": {"latitude": 41.9, "longitude": -87.6},
                        "types": ["store", "establishment"], "addressComponents": [{"shortText": "US", "types": ["country"]}]}]}  # fmt: skip
    with pytest.raises(PlacesError, match="business"):
        await client_for(FakeGoogle(center=shop)).center("Ink Co Chicago")
    with pytest.raises(PlacesError, match="No place matched"):
        await client_for(FakeGoogle(center={"places": []})).center("Nowhere")


@sync
async def test_bad_key_is_a_clear_error() -> None:
    with pytest.raises(PlacesError) as exc:
        await client_for(FakeGoogle(status=403)).center("Chicago, IL")
    assert exc.value.status == 403 and "GOOGLE_PLACES_API_KEY" in exc.value.message


@sync
async def test_search_pages_drops_closed_and_dedupes() -> None:
    pages = [
        {"places": [place(1), place(2, businessStatus="CLOSED_PERMANENTLY")], "nextPageToken": "t2"},
        {"places": [place(1), place(3)]},  # place 1 again: kept once
    ]
    google = FakeGoogle(pages=pages)
    area = await client_for(google).search_area("screen printing", "Chicago, IL", 10, max_searches=20)
    assert sorted(area.places) == ["p1", "p3"]
    assert (area.center_lookups, area.searches, area.stopped_at_limit) == (1, 2, False)
    assert area.cost_usd == round(0.032 + 2 * 0.035, 4)
    p1 = area.places["p1"]
    assert (p1.city, p1.state, p1.country, p1.website_url, p1.review_count) == ("Chicago", "IL", "US", "https://shop1.com/", 30)
    search = google.requests[1]
    assert "places.websiteUri" in search["mask"] and search["body"]["pageSize"] == 20
    assert google.requests[2]["body"]["pageToken"] == "t2"


@sync
async def test_full_circle_splits_until_the_request_limit() -> None:
    full = [{"places": [place(n) for n in range(k * 20, k * 20 + 20)], "nextPageToken": f"t{k}"} for k in range(3)]
    full[2].pop("nextPageToken")
    google = FakeGoogle(pages=full)  # the first circle is full (60): it splits into 9
    area = await client_for(google).search_area("screen printing", "Chicago, IL", 10, max_searches=6)
    # 1 centre lookup + 3 pages of the first circle + 2 of its children, then the limit.
    assert (area.center_lookups, area.searches, area.stopped_at_limit) == (1, 5, True)
    assert len(area.places) == 60
    child_radius = google.requests[4]["body"]["locationBias"]["circle"]["radius"]
    assert child_radius < google.requests[1]["body"]["locationBias"]["circle"]["radius"]


TEXAS = {
    "places": [{
        "displayName": {"text": "Texas"}, "formattedAddress": "Texas, USA", "location": {"latitude": 31.0, "longitude": -100.0},
        "types": ["administrative_area_level_1", "political"], "addressComponents": [{"shortText": "US", "types": ["country"]}],
        "viewport": {"low": {"latitude": 25.8, "longitude": -106.6}, "high": {"latitude": 36.5, "longitude": -93.5}},
    }]
}  # fmt: skip


@sync
async def test_a_state_is_searched_across_its_box_not_around_its_centre() -> None:
    """2026-11-02: "United States" with a 10 mile radius searched one circle in Kansas and found 2."""
    full = [{"places": [place(n) for n in range(k * 20, k * 20 + 20)], "nextPageToken": f"t{k}"} for k in range(3)]
    full[2].pop("nextPageToken")
    google = FakeGoogle(center=TEXAS, pages=full)  # the whole state is full (60): it splits into quarters
    area = await client_for(google).search_area("dtf transfers", "Texas", 10, max_searches=20)
    assert "viewport" in google.requests[0]["mask"]
    first = google.requests[1]["body"]
    assert "locationBias" not in first
    assert first["locationRestriction"]["rectangle"] == {"low": {"latitude": 25.8, "longitude": -106.6},
                                                          "high": {"latitude": 36.5, "longitude": -93.5}}  # fmt: skip
    # 3 pages of the state, then one request for each of its 4 quarters (empty here).
    assert (area.center_lookups, area.searches, area.stopped_at_limit, len(area.places)) == (1, 7, False, 60)
    quarters = [r["body"]["locationRestriction"]["rectangle"] for r in google.requests[4:]]
    assert {(round(q["low"]["latitude"], 4), round(q["low"]["longitude"], 4)) for q in quarters} == {(25.8, -106.6), (25.8, -100.05), (31.15, -106.6), (31.15, -100.05)}
    assert "all of it was searched" in area.notes[0]


def test_a_box_across_the_180th_meridian_splits_correctly() -> None:
    from services.google_places import Box

    alaska = Box(51.0, 172.0, 71.0, -130.0, 0)  # west of the meridian to east of it
    west, east = sorted({(q.west, q.east) for q in split_box(alaska)})
    assert west == (-159.0, -130.0) and east == (172.0, -159.0)


@sync
async def test_each_place_is_looked_up_once_however_many_terms() -> None:
    google = FakeGoogle()
    places = client_for(google)
    a = await places.search_area("dtf transfers", "Chicago, IL", 10, max_searches=5)
    b = await places.search_area("gang sheets", "chicago, il", 10, max_searches=5)
    assert (a.center_lookups, b.center_lookups) == (1, 0)
    assert sum("locationBias" not in r["body"] for r in google.requests) == 1


# ---------- Find endpoint ----------


class FakePlaces:
    def __init__(self, places=None, error: PlacesError | None = None) -> None:
        self.places, self.error = places or {}, error
        self.asked: list[tuple] = []

    async def search_area(self, term, location, radius_miles, max_searches, country="US"):
        from services.google_places import AreaSearch

        self.asked.append((term, location, radius_miles, max_searches))
        if self.error:
            raise self.error
        return AreaSearch(places=dict(self.places), searches=3, center_lookups=1)


def maps_places():
    from services.google_places import _to_place

    return {
        "p1": _to_place(place(1)),
        "p2": _to_place(place(2, websiteUri="https://www.fedex.com/en-us/printing.html")),  # chain: skipped
        "p3": _to_place(place(3, websiteUri="https://booksy.com/shop3")),  # platform: kept, no domain
        "p4": _to_place(place(4, websiteUri="https://www.inkco.com/")),  # already in the pipeline from Apollo
    }


def find_maps(client, **gm):
    return client.post("/api/v1/pipeline/find", json={"icpId": str(ICP_ID), "sources": ["google_maps"], "googleMaps": gm}, headers=WS)


def test_find_on_google_maps(client, env) -> None:  # noqa: F811
    client.post("/api/v1/pipeline/find", json={"icpId": str(ICP_ID), "sources": ["apollo"]}, headers=WS)  # adds inkco.com
    fake = FakePlaces(maps_places())
    app.dependency_overrides[get_optional_places] = lambda: fake
    r = find_maps(client, radiusMiles=5, maxSearches=10).json()["googleMaps"]
    # Defaults come from the ICP's search terms and places.
    assert fake.asked == [("med spa", "Austin, TX", 5, 10)]
    assert (r["found"], r["added"], r["duplicates"], r["searches"], r["costUsd"]) == (4, 2, 1, 4, round(3 * 0.035 + 0.032, 4))
    assert "1 national chain location skipped." in r["notes"]
    rows = {x["name"]: x for x in client.get("/api/v1/pipeline/companies", headers=WS).json() if x["source"] == "google_maps"}
    assert set(rows) == {"Shop 1", "Shop 3"} and rows["Shop 1"]["domain"] == "shop1.com" and rows["Shop 3"]["domain"] is None
    assert rows["Shop 1"]["status"] == "found" and rows["Shop 1"]["phone"] == "(312) 555-0100"
    assert ("google_places", "places:searchText", "succeeded") in env.calls.records
    # Searching again finds the same places: nothing new.
    again = find_maps(client).json()["googleMaps"]
    assert (again["added"], again["duplicates"]) == (0, 3)


def test_find_on_google_maps_problems(client, env) -> None:  # noqa: F811
    app.dependency_overrides[get_optional_places] = lambda: None
    assert find_maps(client).json()["googleMaps"]["error"] == "GOOGLE_PLACES_API_KEY is not set"
    app.dependency_overrides[get_optional_places] = lambda: FakePlaces(error=PlacesError("Google rejected the key.", 403))
    r = find_maps(client).json()["googleMaps"]
    assert r["error"] == "Google rejected the key." and r["added"] == 0
    app.dependency_overrides[get_optional_places] = lambda: FakePlaces(error=PlacesError('"Springfield" resolved to X in PK, not US.'))
    r = find_maps(client, locations=["Springfield"]).json()["googleMaps"]
    assert r["error"] is None and r["notes"] == ['med spa around Springfield: "Springfield" resolved to X in PK, not US.']
    assert find_maps(client, radiusMiles=80).status_code == 422


def test_the_request_limit_is_shared_by_every_term_and_place(client, env) -> None:  # noqa: F811
    """2026-11-02: the first term used the whole limit and the others never ran."""
    fake = FakePlaces(maps_places())
    app.dependency_overrides[get_optional_places] = lambda: fake
    r = find_maps(client, searchTerms=["dtf transfers", "gang sheets", "dtf gang sheets"], locations=["Texas"], maxSearches=30).json()
    # 30 between 3 terms: 10 each (each fake search uses 4 of its share, so the rest passes on).
    assert [a[3] for a in fake.asked] == [10, 13, 22]
    assert r["googleMaps"]["searches"] == 12


def test_phones_outside_the_us_keep_their_country_code() -> None:
    from services.google_places import _to_place

    def raw(country):
        return {"id": "p", "displayName": {"text": "Shop"}, "nationalPhoneNumber": "021 3456789", "internationalPhoneNumber": "+92 21 3456789",
                "addressComponents": [{"shortText": country, "types": ["country"]}]}  # fmt: skip

    assert _to_place(raw("PK")).phone == "+92 21 3456789"
    us = raw("US") | {"nationalPhoneNumber": "(312) 555-0100", "internationalPhoneNumber": "+1 312-555-0100"}
    assert _to_place(us).phone == "(312) 555-0100"
