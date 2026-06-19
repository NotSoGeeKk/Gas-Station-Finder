from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from routing.models import FuelStation
from routing.optimizer import (
    StationCandidate,
    build_route_points,
    plan_fuel_stops,
    NoStationInRangeError,
)


def make_station(name, city, state, price, lat, lng):
    return FuelStation.objects.create(
        opis_id=1,
        name=name,
        address="Test Address",
        city=city,
        state=state,
        rack_id=1,
        retail_price=Decimal(str(price)),
        latitude=lat,
        longitude=lng,
    )


class BuildRoutePointsTests(TestCase):
    def test_cumulative_distance_increases_along_route(self):
        geometry = [(41.8781, -87.6298), (39.7817, -89.6501), (32.7767, -96.7970)]
        points = build_route_points(geometry)
        self.assertEqual(points[0].cumulative_miles, 0.0)
        self.assertGreater(points[1].cumulative_miles, points[0].cumulative_miles)
        self.assertGreater(points[2].cumulative_miles, points[1].cumulative_miles)


class PlanFuelStopsTests(TestCase):
    def test_no_stops_needed_when_within_range(self):
        stops, cost = plan_fuel_stops(total_distance_miles=300, candidates=[], max_range_miles=500, mpg=10)
        self.assertEqual(stops, [])
        self.assertEqual(cost, 0.0)

    def test_picks_cheapest_station_in_range(self):
        cheap = make_station("Cheap Gas", "Town A", "IL", 2.50, 40.0, -89.0)
        pricey = make_station("Pricey Gas", "Town B", "IL", 3.50, 40.0, -89.0)

        candidates = [
            StationCandidate(station=cheap, mile_marker=400, offset_miles=1),
            StationCandidate(station=pricey, mile_marker=450, offset_miles=1),
        ]

        stops, cost = plan_fuel_stops(
            total_distance_miles=900, candidates=candidates, max_range_miles=500, mpg=10
        )

        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].station.name, "Cheap Gas")
        # remaining 500 miles from mile 400 to destination(900) at 2.50/gal, 10mpg
        self.assertAlmostEqual(cost, 500 / 10 * 2.50, places=2)

    def test_tiebreak_prefers_farther_station_at_similar_price(self):
        near = make_station("Near", "Town A", "IL", 3.00, 40.0, -89.0)
        far = make_station("Far", "Town B", "IL", 3.00, 40.0, -89.0)

        candidates = [
            StationCandidate(station=near, mile_marker=300, offset_miles=1),
            StationCandidate(station=far, mile_marker=490, offset_miles=1),
        ]

        stops, cost = plan_fuel_stops(
            total_distance_miles=900, candidates=candidates, max_range_miles=500, mpg=10
        )

        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].station.name, "Far")

    def test_raises_when_no_station_reachable(self):
        far_away = make_station("TooFar", "Town X", "IL", 3.00, 40.0, -89.0)
        candidates = [StationCandidate(station=far_away, mile_marker=600, offset_miles=1)]

        with self.assertRaises(NoStationInRangeError):
            plan_fuel_stops(
                total_distance_miles=900, candidates=candidates, max_range_miles=500, mpg=10
            )

    def test_multi_stop_trip(self):
        s1 = make_station("Stop1", "A", "IL", 3.00, 40.0, -89.0)
        s2 = make_station("Stop2", "B", "IL", 2.80, 40.0, -89.0)

        candidates = [
            StationCandidate(station=s1, mile_marker=480, offset_miles=1),
            StationCandidate(station=s2, mile_marker=960, offset_miles=1),
        ]

        stops, cost = plan_fuel_stops(
            total_distance_miles=1400, candidates=candidates, max_range_miles=500, mpg=10
        )

        self.assertEqual(len(stops), 2)
        # First leg from start (free) to stop1 funds 480->960 = 480 miles @3.00
        # Second stop funds 960 -> 1400 = 440 miles @2.80
        expected = (480 / 10 * 3.00) + (440 / 10 * 2.80)
        self.assertAlmostEqual(cost, expected, places=2)


class PlanRouteAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        make_station("Cheap Gas", "Springfield", "MO", 2.90, 37.2090, -93.2923)

    @patch("routing.views.get_route")
    @patch("routing.views.geocode_location")
    def test_route_endpoint_returns_expected_shape(self, mock_geocode, mock_get_route):
        mock_geocode.side_effect = [(41.8781, -87.6298), (32.7767, -96.7970)]
        mock_get_route.return_value = {
            "distance_miles": 941.15,
            "duration_seconds": 50000,
            "geometry": [
                (41.8781, -87.6298), (39.7817, -89.6501),
                (37.2090, -93.2923), (32.7767, -96.7970),
            ],
        }

        response = self.client.post(
            "/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("distance_miles", body)
        self.assertIn("fuel_stops", body)
        self.assertIn("total_fuel_cost_usd", body)
        self.assertIn("route_geometry", body)

    def test_invalid_payload_returns_400(self):
        response = self.client.post("/api/route/", {}, format="json")
        self.assertEqual(response.status_code, 400)
