import json
import logging
import urllib.parse

from django.shortcuts import render
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiExample
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from routing.osrm_client import geocode_location, get_route, GeocodingError, RoutingError
from routing.optimizer import (
    build_route_points,
    find_candidates_near_route,
    plan_fuel_stops,
    NoStationInRangeError,
)
from routing.serializers import RouteRequestSerializer, RouteResponseSerializer

logger = logging.getLogger(__name__)


def _build_map_page_url(request, start: str, finish: str, max_range_miles: float, mpg: float) -> str:
    """Return the absolute URL for the interactive Leaflet map page."""
    params = urllib.parse.urlencode({
        "start": start,
        "finish": finish,
        "max_range_miles": max_range_miles,
        "mpg": mpg,
    })
    return request.build_absolute_uri(f"/api/map/?{params}")


@extend_schema(
    summary="Health check",
    description="Liveness probe — returns `{\"status\": \"ok\"}` when the server is up.",
    responses={200: {"type": "object", "properties": {"status": {"type": "string", "example": "ok"}}}},
)
@api_view(["GET"])
def health(request):
    """GET /api/health/ - simple liveness check."""
    return Response({"status": "ok"})


def _run_route_pipeline(start_query: str, finish_query: str, max_range_miles: float, mpg: float):
    """Shared planning logic used by both the JSON API and the map view.

    Returns a dict ready for either serialisation or template rendering,
    or raises GeocodingError / RoutingError / NoStationInRangeError.
    """
    start_coords = geocode_location(start_query)
    finish_coords = geocode_location(finish_query)
    route = get_route(start_coords, finish_coords)

    route_points = build_route_points(route["geometry"])
    candidates = find_candidates_near_route(route_points)
    stops, total_cost = plan_fuel_stops(
        total_distance_miles=route["distance_miles"],
        candidates=candidates,
        max_range_miles=max_range_miles,
        mpg=mpg,
    )

    fuel_stops_payload = [
        {
            "name": s.station.name,
            "address": s.station.address,
            "city": s.station.city,
            "state": s.station.state,
            "price_per_gallon": float(s.station.retail_price),
            "latitude": s.station.latitude,
            "longitude": s.station.longitude,
            "mile_marker": round(s.mile_marker, 1),
            "gallons_purchased": s.gallons_purchased,
            "cost": s.cost,
        }
        for s in stops
    ]

    geometry_points = [[p.lat, p.lng] for p in route_points]

    return {
        "start": {"query": start_query, "latitude": start_coords[0], "longitude": start_coords[1]},
        "finish": {"query": finish_query, "latitude": finish_coords[0], "longitude": finish_coords[1]},
        "distance_miles": round(route["distance_miles"], 1),
        "duration_hours": round(route["duration_seconds"] / 3600, 2),
        "max_range_miles": max_range_miles,
        "mpg": mpg,
        "total_fuel_cost_usd": total_cost,
        "number_of_fuel_stops": len(stops),
        "fuel_stops": fuel_stops_payload,
        "route_geometry": geometry_points,
    }


@extend_schema(
    summary="Plan a fuel-optimised route",
    description=(
        "Given a start and finish location within the USA, returns the driving route, "
        "an optimal (lowest-cost) sequence of fuel stops respecting the vehicle's maximum range, "
        "and the total money spent on fuel.\n\n"
        "**External API calls:** 0–2 Nominatim geocoding calls (skipped for raw lat,lng) "
        "+ exactly 1 OSRM routing call. Fuel data is served from local DB — no extra calls."
    ),
    request=RouteRequestSerializer,
    responses={200: RouteResponseSerializer},
    examples=[
        OpenApiExample(
            "Chicago to Dallas",
            value={"start": "Chicago, IL", "finish": "Dallas, TX", "max_range_miles": 500, "mpg": 10},
            request_only=True,
        ),
        OpenApiExample(
            "Using raw coordinates",
            value={"start": "41.8781,-87.6298", "finish": "32.7767,-96.7970"},
            request_only=True,
        ),
    ],
)
@api_view(["POST"])
def plan_route(request):
    """POST /api/route/

    Body: {"start": "Chicago, IL", "finish": "Dallas, TX",
           "max_range_miles": 500, "mpg": 10}

    Returns the driving route, an optimal sequence of fuel stops along it
    (cheapest reachable price, minimizing stop count as a tiebreaker), and
    the total fuel cost for the trip.

    External API usage: at most one geocoding call per free-text location
    (skipped entirely if "lat,lng" is supplied directly) plus exactly one
    routing call to OSRM. Fuel station lookup and stop planning are pure
    DB/Python and incur no external calls.
    """
    serializer = RouteRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        result = _run_route_pipeline(
            start_query=data["start"],
            finish_query=data["finish"],
            max_range_miles=data["max_range_miles"],
            mpg=data["mpg"],
        )
    except GeocodingError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except RoutingError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    except NoStationInRangeError as exc:
        return Response(
            {
                "error": str(exc),
                "detail": (
                    "The route has a stretch longer than the vehicle's range "
                    "with no fuel station data available near it."
                ),
            },
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    result["map_url"] = _build_map_page_url(
        request,
        start=data["start"],
        finish=data["finish"],
        max_range_miles=data["max_range_miles"],
        mpg=data["mpg"],
    )
    return Response(result, status=status.HTTP_200_OK)


def map_view(request):
    """GET /api/map/?start=Chicago, IL&finish=Dallas, TX

    Renders an interactive Leaflet map showing the driving route polyline,
    start/finish pins, and numbered fuel-stop markers with cost details.
    Accepts the same query parameters as POST /api/route/.
    """
    start = request.GET.get("start", "").strip()
    finish = request.GET.get("finish", "").strip()

    if not start or not finish:
        return render(request, "routing/map_error.html", {
            "error": "Both 'start' and 'finish' query parameters are required.",
        }, status=400)

    try:
        max_range_miles = float(request.GET.get("max_range_miles", 500).strip().strip('"\''))
        mpg = float(request.GET.get("mpg", 10).strip().strip('"\''))
    except (ValueError, AttributeError):
        return render(request, "routing/map_error.html", {
            "error": "'max_range_miles' and 'mpg' must be numbers.",
        }, status=400)

    try:
        result = _run_route_pipeline(
            start_query=start,
            finish_query=finish,
            max_range_miles=max_range_miles,
            mpg=mpg,
        )
    except GeocodingError as exc:
        return render(request, "routing/map_error.html", {"error": str(exc)}, status=400)
    except RoutingError as exc:
        return render(request, "routing/map_error.html", {"error": str(exc)}, status=502)
    except NoStationInRangeError as exc:
        return render(request, "routing/map_error.html", {"error": str(exc)}, status=422)

    ctx = {
        "start_query": result["start"]["query"],
        "finish_query": result["finish"]["query"],
        "distance_miles": result["distance_miles"],
        "duration_hours": result["duration_hours"],
        "max_range_miles": result["max_range_miles"],
        "mpg": result["mpg"],
        "total_fuel_cost_usd": result["total_fuel_cost_usd"],
        "number_of_fuel_stops": result["number_of_fuel_stops"],
        # JSON strings injected directly into <script> tags
        "route_geometry_json": json.dumps(result["route_geometry"]),
        "fuel_stops_json": json.dumps(result["fuel_stops"]),
        "start_json": json.dumps(result["start"]),
        "finish_json": json.dumps(result["finish"]),
    }
    return render(request, "routing/map.html", ctx)
