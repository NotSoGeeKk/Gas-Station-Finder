"""Thin client around the free, keyless OSRM public routing API.

OSRM (Open Source Routing Machine) - http://project-osrm.org/
The public demo server requires no API key and returns route geometry,
distance, and duration in a single HTTP call, which satisfies the
requirement to minimize external routing API usage (one call per request).
"""

import requests
from django.conf import settings

import polyline as polyline_lib


class GeocodingError(Exception):
    """Raised when a place name cannot be resolved to coordinates."""


class RoutingError(Exception):
    """Raised when OSRM cannot compute a route between two points."""


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"


# Approximate bounding box of the contiguous USA + Alaska + Hawaii.
# Used only to validate raw "lat,lng" inputs — free-text geocoding is
# already US-restricted via Nominatim's countrycodes=us parameter.
_US_LAT_MIN, _US_LAT_MAX = 18.0, 72.0
_US_LNG_MIN, _US_LNG_MAX = -180.0, -66.0


def geocode_location(place: str) -> tuple[float, float]:
    """Resolve a free-text place name (e.g. 'Dallas, TX') to (lat, lng)
    using OSM Nominatim. Accepts 'lat,lng' directly to skip the network
    call entirely if the caller already has coordinates.
    """
    place = place.strip()

    # Allow direct "lat,lng" input to avoid a geocoding call altogether.
    if "," in place:
        parts = place.split(",")
        if len(parts) == 2:
            try:
                lat, lng = float(parts[0]), float(parts[1])
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    if not (_US_LAT_MIN <= lat <= _US_LAT_MAX and _US_LNG_MIN <= lng <= _US_LNG_MAX):
                        raise GeocodingError(
                            f"Coordinates ({lat}, {lng}) are outside the United States. "
                            "Both start and finish must be within the USA."
                        )
                    return lat, lng
            except ValueError:
                pass

    resp = requests.get(
        NOMINATIM_URL,
        params={
            "q": place,
            "format": "json",
            "limit": 1,
            "countrycodes": "us",
        },
        headers={"User-Agent": "fuel-route-api/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise GeocodingError(f"Could not geocode location: {place!r}")

    return float(results[0]["lat"]), float(results[0]["lon"])


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> dict:
    """Fetch a driving route from OSRM. One HTTP call.

    Returns a dict with:
      - distance_miles: total route distance
      - duration_seconds: estimated driving time
      - geometry: list of (lat, lng) points describing the route polyline
    """
    coords = f"{start[1]},{start[0]};{finish[1]},{finish[0]}"
    url = OSRM_URL.format(coords=coords)

    resp = requests.get(
        url,
        params={
            "overview": "full",
            "geometries": "polyline",
            "steps": "false",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"OSRM could not find a route: {data.get('message', 'unknown error')}")

    route = data["routes"][0]
    geometry = polyline_lib.decode(route["geometry"])  # list[(lat, lng)]

    meters = route["distance"]
    miles = meters / 1609.344

    return {
        "distance_miles": miles,
        "duration_seconds": route["duration"],
        "geometry": geometry,
    }
