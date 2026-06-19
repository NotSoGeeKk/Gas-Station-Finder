from django.urls import path

from routing.views import health, map_view, plan_route

urlpatterns = [
    path("route/", plan_route, name="plan-route"),
    path("health/", health, name="health"),
    path("map/", map_view, name="map-view"),
]
