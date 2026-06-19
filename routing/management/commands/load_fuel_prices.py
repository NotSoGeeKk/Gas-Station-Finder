import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import FuelStation

DATA_DIR = Path(settings.BASE_DIR) / "routing" / "data"
CSV_PATH = DATA_DIR / "fuel_prices.csv"
GEO_CACHE_PATH = DATA_DIR / "city_geo_cache.json"


class Command(BaseCommand):
    help = (
        "Load fuel station prices from the provided CSV and resolve each "
        "station's latitude/longitude from an offline city/state geocode "
        "cache (no external geocoding API calls)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=str(CSV_PATH),
            help="Path to the fuel prices CSV file.",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing FuelStation rows before loading.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            self.stderr.write(f"CSV not found at {csv_path}")
            return

        with open(GEO_CACHE_PATH) as f:
            geo_cache = json.load(f)

        if options["flush"]:
            FuelStation.objects.all().delete()
            self.stdout.write("Flushed existing FuelStation rows.")

        stations = []
        matched, unmatched = 0, 0
        unmatched_examples = []

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                city = row["City"].strip()
                state = row["State"].strip()
                key = f"{city.lower()}|{state}"
                coords = geo_cache.get(key)

                if coords:
                    lat, lng = coords
                    matched += 1
                else:
                    lat, lng = None, None
                    unmatched += 1
                    if len(unmatched_examples) < 10:
                        unmatched_examples.append(f"{city}, {state}")

                try:
                    price = float(row["Retail Price"])
                except (ValueError, KeyError):
                    continue

                stations.append(
                    FuelStation(
                        opis_id=int(row["OPIS Truckstop ID"]),
                        name=row["Truckstop Name"].strip(),
                        address=row["Address"].strip(),
                        city=city,
                        state=state,
                        rack_id=int(row["Rack ID"]) if row["Rack ID"] else 0,
                        retail_price=price,
                        latitude=lat,
                        longitude=lng,
                    )
                )

        with transaction.atomic():
            FuelStation.objects.bulk_create(stations, batch_size=2000)

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(stations)} fuel stations "
                f"({matched} geocoded, {unmatched} without coordinates)."
            )
        )
        if unmatched_examples:
            self.stdout.write(
                "Examples of unmatched city/state (excluded from route "
                "matching, e.g. non-US or unrecognized names): "
                + ", ".join(unmatched_examples)
            )
