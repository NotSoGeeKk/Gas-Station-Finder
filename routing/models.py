from django.db import models


class FuelStation(models.Model):
    """A truckstop fuel pricing record, loaded once from the provided CSV
    via the `load_fuel_prices` management command. Coordinates are resolved
    at load time from an offline city/state geocode cache, so no geocoding
    API calls happen at request time.
    """

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField()
    retail_price = models.DecimalField(max_digits=8, decimal_places=5)

    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.retail_price}"
