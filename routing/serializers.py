from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        help_text="Start location: 'City, ST' or 'lat,lng'.",
        max_length=255,
    )
    finish = serializers.CharField(
        help_text="Finish location: 'City, ST' or 'lat,lng'.",
        max_length=255,
    )
    max_range_miles = serializers.FloatField(default=500.0, min_value=1)
    mpg = serializers.FloatField(default=10.0, min_value=0.1)


class FuelStopSerializer(serializers.Serializer):
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    price_per_gallon = serializers.FloatField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    mile_marker = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost = serializers.FloatField()


class RouteResponseSerializer(serializers.Serializer):
    start = serializers.DictField()
    finish = serializers.DictField()
    distance_miles = serializers.FloatField()
    duration_hours = serializers.FloatField()
    max_range_miles = serializers.FloatField()
    mpg = serializers.FloatField()
    total_fuel_cost_usd = serializers.FloatField()
    number_of_fuel_stops = serializers.IntegerField()
    fuel_stops = FuelStopSerializer(many=True)
    route_geometry = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField()),
        help_text="Polyline as [[lat, lng], ...] for map rendering.",
    )
    map_url = serializers.CharField(
        help_text="Pre-built OSM/Google static map URL embedding the route.",
        required=False,
    )
