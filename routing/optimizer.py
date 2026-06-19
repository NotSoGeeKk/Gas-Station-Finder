"""Fuel stop optimization.

Given a route polyline (list of lat/lng points from OSRM) and the set of
FuelStation records, this module:

1. Builds a cumulative-mileage index along the route.
2. Projects nearby fuel stations onto that index (their approximate
   mile-marker position along the route + perpendicular distance from it).
3. Greedily chooses refuel stops so the vehicle never exceeds its max
   range, preferring the cheapest price among reachable candidates and
   using "go as far as possible" as a tiebreaker to minimize stop count.

All of this is plain Python/SQL - no external API calls are made here.
"""

import math
from dataclasses import dataclass

import numpy as np

from routing.models import FuelStation

EARTH_RADIUS_MILES = 3958.8

# How far off the driving route (in miles, great-circle approximation) a
# station's city center may be and still be considered "on route". Truck
# stops are typically right off the highway, but our geocoding is at
# city-level granularity, so a generous corridor avoids false negatives.
CORRIDOR_MILES = 12

# Only evaluate stations whose city falls within this lat/lng bounding box
# of the route's overall bounding box, expanded by the corridor — this is
# a cheap pre-filter before the more expensive point-to-polyline math.
BBOX_PAD_DEGREES = 0.5


def haversine_miles(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


@dataclass
class RoutePoint:
    lat: float
    lng: float
    cumulative_miles: float


@dataclass
class StationCandidate:
    station: FuelStation
    mile_marker: float  # distance along the route where this station sits
    offset_miles: float  # perpendicular-ish distance off the route


@dataclass
class FuelStop:
    station: FuelStation
    mile_marker: float
    gallons_purchased: float
    cost: float


def build_route_points(geometry: list[tuple[float, float]]) -> list[RoutePoint]:
    """Densify the OSRM polyline into RoutePoints carrying cumulative
    distance along the route, so any subsequent point can be located by
    mile-marker via simple lookup/interpolation.
    """
    points = []
    cumulative = 0.0
    prev = None
    for lat, lng in geometry:
        if prev is not None:
            cumulative += haversine_miles(prev, (lat, lng))
        points.append(RoutePoint(lat=lat, lng=lng, cumulative_miles=cumulative))
        prev = (lat, lng)
    return points


def _route_bbox(points: list[RoutePoint]) -> tuple[float, float, float, float]:
    lats = [p.lat for p in points]
    lngs = [p.lng for p in points]
    return min(lats), max(lats), min(lngs), max(lngs)


def _sample_by_distance(points: list[RoutePoint], interval_miles: float) -> list[RoutePoint]:
    """Down-sample route points so consecutive samples are roughly
    `interval_miles` apart, regardless of how dense the source polyline
    is. Always keeps the first and last point.
    """
    if not points:
        return []

    sampled = [points[0]]
    next_threshold = interval_miles
    for p in points[1:]:
        if p.cumulative_miles >= next_threshold:
            sampled.append(p)
            next_threshold += interval_miles

    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])

    return sampled


def find_candidates_near_route(points: list[RoutePoint]) -> list[StationCandidate]:
    """Find fuel stations near the route polyline.

    Step 1: a cheap DB-level bounding-box query to avoid pulling every
    station in the country into Python.
    Step 2: for the (much smaller) remaining set, project each station
    onto the nearest route point to get its mile-marker and corridor
    offset, discarding anything outside CORRIDOR_MILES. This step is
    vectorized with numpy so it stays fast even on cross-country routes
    with thousands of polyline points and thousands of nearby stations.
    """
    min_lat, max_lat, min_lng, max_lng = _route_bbox(points)

    candidates_qs = list(
        FuelStation.objects.filter(
            latitude__isnull=False,
            longitude__isnull=False,
            latitude__gte=min_lat - BBOX_PAD_DEGREES,
            latitude__lte=max_lat + BBOX_PAD_DEGREES,
            longitude__gte=min_lng - BBOX_PAD_DEGREES,
            longitude__lte=max_lng + BBOX_PAD_DEGREES,
        ).only(
            "id", "opis_id", "name", "address", "city", "state",
            "retail_price", "latitude", "longitude",
        )
    )

    if not candidates_qs:
        return []

    # Sample the route roughly every few miles rather than by raw point
    # count - OSRM polylines can have a point every few dozen meters, far
    # denser than needed for a 12-mile corridor check, and sampling by
    # distance keeps the comparison matrix small regardless of polyline
    # density or route length.
    sampled = _sample_by_distance(points, interval_miles=3.0)

    station_lat = np.radians(np.array([s.latitude for s in candidates_qs]))
    station_lng = np.radians(np.array([s.longitude for s in candidates_qs]))
    route_lat = np.radians(np.array([p.lat for p in sampled]))
    route_lng = np.radians(np.array([p.lng for p in sampled]))
    route_cum = np.array([p.cumulative_miles for p in sampled])

    # Haversine distance, vectorized: stations x route_points matrix.
    dlat = route_lat[None, :] - station_lat[:, None]
    dlng = route_lng[None, :] - station_lng[:, None]
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(station_lat[:, None]) * np.cos(route_lat[None, :]) * np.sin(dlng / 2) ** 2
    )
    dist_matrix = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    nearest_idx = np.argmin(dist_matrix, axis=1)
    nearest_dist = dist_matrix[np.arange(len(candidates_qs)), nearest_idx]

    results = []
    for i, station in enumerate(candidates_qs):
        if nearest_dist[i] <= CORRIDOR_MILES:
            results.append(
                StationCandidate(
                    station=station,
                    mile_marker=float(route_cum[nearest_idx[i]]),
                    offset_miles=float(nearest_dist[i]),
                )
            )

    results.sort(key=lambda c: c.mile_marker)
    return results


def plan_fuel_stops(
    total_distance_miles: float,
    candidates: list[StationCandidate],
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
) -> tuple[list[FuelStop], float]:
    """Greedy fuel-stop planner.

    Strategy: starting with a full tank (max_range_miles of range), repeatedly
    consider every candidate station reachable within the current range.
    Among those, pick the cheapest price per gallon; if several are tied
    (within a cent), prefer the one furthest along the route, since that
    reduces the number of stops without increasing cost. Stop once the
    destination itself is within range of the current position.

    Cost accounting: each fill-up tops the tank back up to max_range_miles
    of range. The price paid at a given stop funds the driving distance
    from that stop to the next one (or to the destination, for the last
    stop). The very first leg, from the trip's start to the first fuel
    stop, runs on fuel the vehicle already had before the trip began and
    is not counted as spend on this trip.

    Returns (list_of_fuel_stops, total_cost).
    """
    if total_distance_miles <= max_range_miles:
        return [], 0.0

    stops: list[FuelStop] = []
    mile_markers: list[float] = []
    chosen_stations: list[FuelStation] = []

    position = 0.0
    remaining_candidates = list(candidates)

    while total_distance_miles - position > max_range_miles:
        reachable = [
            c for c in remaining_candidates
            if c.mile_marker > position and c.mile_marker <= position + max_range_miles
        ]

        if not reachable:
            raise NoStationInRangeError(
                f"No fuel station found within {max_range_miles} miles of "
                f"mile marker {position:.1f}."
            )

        min_price = min(float(c.station.retail_price) for c in reachable)
        best_in_price_band = [
            c for c in reachable
            if float(c.station.retail_price) <= min_price + 0.01
        ]
        chosen = max(best_in_price_band, key=lambda c: c.mile_marker)

        mile_markers.append(chosen.mile_marker)
        chosen_stations.append(chosen.station)
        position = chosen.mile_marker
        remaining_candidates = [c for c in remaining_candidates if c.mile_marker > position]

    # Walk forward computing the cost of the leg each stop funds: stop i
    # pays for the distance from mile_markers[i] to mile_markers[i+1] (or
    # to the destination for the last stop).
    total_cost = 0.0
    for i, (marker, station) in enumerate(zip(mile_markers, chosen_stations)):
        leg_end = mile_markers[i + 1] if i + 1 < len(mile_markers) else total_distance_miles
        leg_miles = leg_end - marker
        leg_cost = round((leg_miles / mpg) * float(station.retail_price), 2)
        total_cost += leg_cost
        stops.append(
            FuelStop(
                station=station,
                mile_marker=marker,
                gallons_purchased=round(max_range_miles / mpg, 3),
                cost=leg_cost,
            )
        )

    return stops, round(total_cost, 2)


class NoStationInRangeError(Exception):
    pass
