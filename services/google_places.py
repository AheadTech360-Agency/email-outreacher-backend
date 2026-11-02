"""Google Maps company search through the Places API (New), ported from Lead Intel's discovery.

Lead Intel doesn't scrape Google Maps: it calls Google's own Places Text Search, over a grid of
points around a place. The same here:

1. The place ("Miami, FL") resolves to a centre point, guarded so a vague name can't land on a
   business or in another country.
2. A grid of circles covers the radius around it. Each circle is searched for the term, up to
   3 pages of 20 (Google never returns more than 60 for one search).
3. A circle that comes back full may hold more than Google showed, so it splits into smaller
   circles, at most 3 levels deep.

A whole state or country is different: a radius around its centre point would search one small
patch in the middle of it ("United States", 10 miles, is a field in Kansas). So a state or country
is searched inside its own boundary box instead, and a box that comes back full splits into
quarters, as deep as the request budget allows. The radius doesn't apply there.

Google charges per request, not per result, so every request counts against `max_searches` and
the run stops cleanly when it's reached. Prices are Lead Intel's, verified against Google's
pricing pages when it was built (packages/shared/src/pricing.ts):
  Text Search Enterprise (the business search, with phone/website/rating)  $35 per 1,000 requests
  Text Search Pro (the centre lookup)                                      $32 per 1,000 requests
"""

import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.identity import shared_domain  # noqa: F401  (re-exported: the find route imports it from here)

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
SEARCH_COST_USD = 0.035
CENTER_COST_USD = 0.032

# Never widen casually: each field can move the request to a dearer SKU.
SEARCH_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.location", "places.nationalPhoneNumber", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "places.primaryType", "nextPageToken",
])  # fmt: skip
# Pro-tier only: the centre needs no phone or website, and asking would cost more for nothing.
CENTER_FIELDS = "places.displayName,places.formattedAddress,places.addressComponents,places.location,places.types,places.viewport"

# Google types that describe an area. A centre must be one, and must not be a business.
AREA_TYPES = {
    "locality", "sublocality", "sublocality_level_1", "neighborhood", "postal_code",
    "administrative_area_level_1", "administrative_area_level_2", "administrative_area_level_3",
    "political", "country",
}  # fmt: skip

EARTH_RADIUS_M = 6_371_000
METERS_PER_MILE = 1609.344
MAX_CELL_RADIUS_M = 50_000  # Places' hard limit on a circle's radius
INITIAL_CELL_RADIUS_M = 24_000  # about 15 miles
MAX_DEPTH = 3
# Splitting a full circle: this ratio tiles it with about 9 smaller ones (Lead Intel measured it;
# 4 half-size circles leave gaps at the edges).
SPLIT_RATIO = math.sqrt(math.pi / 8)
PAGE_SIZE = 20
MAX_PAGES = 3
# A state or country is searched by its boundary box, not a radius.
WIDE_TYPES = {"country", "administrative_area_level_1"}
# A full box splits into 4; 6 levels is 4,096 boxes, far past any budget, so the budget is the real limit.
MAX_BOX_DEPTH = 6


class PlacesError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Cell:
    lat: float
    lng: float
    radius_m: float
    depth: int


@dataclass
class Box:
    """A boundary box. `east` may be less than `west` when it crosses the 180th meridian (Alaska)."""

    south: float
    west: float
    north: float
    east: float
    depth: int


@dataclass
class Area:
    lat: float
    lng: float
    label: str
    box: Box | None  # set for a state or country: searched by box instead of by radius


@dataclass
class Place:
    place_id: str
    name: str
    website_url: str | None
    phone: str | None
    address: str | None
    city: str | None
    state: str | None
    country: str | None
    rating: float | None
    review_count: int | None
    business_status: str | None
    primary_type: str | None
    raw: dict[str, Any]


@dataclass
class AreaSearch:
    """What one term searched around one place found, and what it cost."""

    places: dict[str, Place] = field(default_factory=dict)
    searches: int = 0
    center_lookups: int = 0
    # Circles that were full at the deepest split: Google may have had more there than it showed.
    capped_cells: int = 0
    stopped_at_limit: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(self.searches * SEARCH_COST_USD + self.center_lookups * CENTER_COST_USD, 4)


# ---------- Grid math (pure) ----------


def _destination(lat: float, lng: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    d = distance_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    lat1, lng1 = math.radians(lat), math.radians(lng)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lng2 = lng1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1, math.sqrt(h)))


def _tile(lat: float, lng: float, target_m: float, cell_m: float, depth: int) -> list[Cell]:
    """Circles of radius cell_m covering a circle of radius target_m, with no gaps: on a square grid
    spaced cell_m * sqrt(2), each circle reaches its square's corners."""
    if cell_m >= target_m:
        return [Cell(lat, lng, cell_m, depth)]
    spacing = cell_m * math.sqrt(2)
    steps = math.ceil((target_m + cell_m) / spacing)
    cells = []
    for row in range(-steps, steps + 1):
        for col in range(-steps, steps + 1):
            north, east = row * spacing, col * spacing
            dist = math.hypot(north, east)
            c_lat, c_lng = (lat, lng) if dist == 0 else _destination(lat, lng, dist, math.degrees(math.atan2(east, north)))
            if _distance_m(lat, lng, c_lat, c_lng) <= target_m + cell_m:
                cells.append(Cell(c_lat, c_lng, cell_m, depth))
    return cells


def initial_grid(lat: float, lng: float, radius_miles: float) -> list[Cell]:
    target = radius_miles * METERS_PER_MILE
    cell = min(INITIAL_CELL_RADIUS_M, MAX_CELL_RADIUS_M, max(target, 1))
    return _tile(lat, lng, target, cell, 0)


def split(cell: Cell) -> list[Cell]:
    if cell.depth >= MAX_DEPTH:
        return []
    return _tile(cell.lat, cell.lng, cell.radius_m, cell.radius_m * SPLIT_RATIO, cell.depth + 1)


def _lng(x: float) -> float:
    return (x + 540) % 360 - 180


def split_box(box: Box) -> list[Box]:
    """Quarters of a full box."""
    if box.depth >= MAX_BOX_DEPTH:
        return []
    east = box.east + 360 if box.east < box.west else box.east
    mid_lat, mid_lng = (box.south + box.north) / 2, _lng((box.west + east) / 2)
    return [
        Box(s, w, n, e, box.depth + 1)
        for s, n in ((box.south, mid_lat), (mid_lat, box.north))
        for w, e in ((box.west, mid_lng), (mid_lng, box.east))
    ]


def _box(raw: dict[str, Any] | None) -> Box | None:
    low, high = (raw or {}).get("low"), (raw or {}).get("high")
    if not low or not high:
        return None
    return Box(low["latitude"], low["longitude"], high["latitude"], high["longitude"], 0)


# ---------- Client ----------


def _component(place: dict[str, Any], kind: str, variant: str) -> str | None:
    for c in place.get("addressComponents") or []:
        if kind in (c.get("types") or []):
            return c.get(variant)
    return None


def _to_place(raw: dict[str, Any]) -> Place | None:
    name = ((raw.get("displayName") or {}).get("text") or "").strip()
    if not name or not raw.get("id"):
        return None
    status = raw.get("businessStatus")
    return Place(
        place_id=raw["id"],
        name=name,
        website_url=(raw.get("websiteUri") or "").strip() or None,
        phone=raw.get("nationalPhoneNumber"),
        address=raw.get("formattedAddress"),
        city=_component(raw, "locality", "longText"),
        state=_component(raw, "administrative_area_level_1", "shortText"),
        country=_component(raw, "country", "shortText"),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount"),
        business_status=status.lower() if isinstance(status, str) else None,
        primary_type=raw.get("primaryType"),
        raw=raw,
    )


def _message(response: httpx.Response) -> str:
    try:
        detail = (response.json().get("error") or {}).get("message")
    except ValueError:
        detail = None
    if response.status_code in (401, 403):
        return f"Google rejected the key. Check GOOGLE_PLACES_API_KEY and that the Places API (New) is enabled. {detail or ''}".strip()
    if response.status_code == 429:
        return "Google's rate limit or daily quota was reached. Try again later."
    return f"Google answered {response.status_code}: {detail or 'no details'}"


class PlacesClient:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None, endpoint: str = ENDPOINT) -> None:
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(timeout=20, transport=transport, headers={"X-Goog-Api-Key": api_key})
        self._areas: dict[tuple[str, str], Area] = {}  # one lookup per place, however many terms search it

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, fields: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(self._endpoint, json=body, headers={"X-Goog-FieldMask": fields})
        except httpx.HTTPError as exc:
            raise PlacesError(f"Couldn't reach Google Places: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise PlacesError(_message(response), response.status_code)
        return response.json()

    async def center(self, location: str, country: str = "US") -> tuple[float, float, str]:
        area = await self.area(location, country)
        return area.lat, area.lng, area.label

    async def area(self, location: str, country: str = "US") -> Area:
        """The centre point of a city, state or postcode, and for a state or country its boundary box.
        Raises PlacesError when it resolves to a business or to somewhere outside `country`, rather
        than searching the wrong place."""
        data = await self._post(CENTER_FIELDS, {"textQuery": location, "pageSize": 1, "regionCode": country})
        found = (data.get("places") or [None])[0]
        if not found or not found.get("location"):
            raise PlacesError(f'No place matched "{location}".')
        name = (found.get("displayName") or {}).get("text") or location
        resolved = (_component(found, "country", "shortText") or "").upper()
        if resolved != country.upper():
            raise PlacesError(f'"{location}" resolved to {name}{f" in {resolved}" if resolved else ""}, not {country}. Add a state, e.g. "Springfield, IL".')
        types = set(found.get("types") or [])
        if "establishment" in types or not types & AREA_TYPES:
            raise PlacesError(f'"{location}" resolved to a business ({name}), not a city or area. Try a city, state or postcode.')
        loc = found["location"]
        box = _box(found.get("viewport")) if types & WIDE_TYPES else None
        return Area(loc["latitude"], loc["longitude"], found.get("formattedAddress") or name, box)

    async def search_page(self, term: str, cell: Cell | Box, page_token: str | None = None) -> tuple[list[Place], str | None]:
        body: dict[str, Any] = {"textQuery": term, "pageSize": PAGE_SIZE}
        if isinstance(cell, Box):
            # A restriction, not a bias: a box search must not drift outside the state.
            body["locationRestriction"] = {"rectangle": {
                "low": {"latitude": cell.south, "longitude": cell.west}, "high": {"latitude": cell.north, "longitude": cell.east},
            }}  # fmt: skip
        else:
            body["locationBias"] = {"circle": {"center": {"latitude": cell.lat, "longitude": cell.lng}, "radius": cell.radius_m}}
        if page_token:
            body["pageToken"] = page_token
        data = await self._post(SEARCH_FIELDS, body)
        places = [p for raw in data.get("places") or [] if (p := _to_place(raw))]
        return places, data.get("nextPageToken")

    async def search_area(
        self, term: str, location: str, radius_miles: float, max_searches: int, country: str = "US"
    ) -> AreaSearch:
        """Search `term` across a grid around `location`, or across its box for a state or country. Stops
        at `max_searches` requests (the centre lookup included), keeping everything found so far.
        Permanently closed places are dropped."""
        out = AreaSearch()
        key = (location.strip().lower(), country)
        if key not in self._areas:
            out.center_lookups = 1
            self._areas[key] = await self.area(location, country)
        area = self._areas[key]
        queue: list[Cell | Box] = [area.box] if area.box else list(initial_grid(area.lat, area.lng, radius_miles))
        if area.box:
            out.notes.append(f"{location} is a whole state or country, so all of it was searched (the radius doesn't apply).")
        while queue:
            cell = queue.pop(0)
            found = 0
            token: str | None = None
            for _ in range(MAX_PAGES):
                if out.searches + out.center_lookups >= max_searches:
                    out.stopped_at_limit = True
                    return out
                try:
                    places, token = await self.search_page(term, cell, token)
                finally:
                    out.searches += 1
                found += len(places)
                for p in places:
                    if p.business_status != "closed_permanently":
                        out.places.setdefault(p.place_id, p)
                if not token:
                    break
            if found >= PAGE_SIZE * MAX_PAGES:
                children = split_box(cell) if isinstance(cell, Box) else split(cell)
                if children:
                    queue.extend(children)
                else:
                    out.capped_cells += 1
        return out
