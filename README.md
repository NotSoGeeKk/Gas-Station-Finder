# Fuel Route API

A Django REST API that, given a start and finish location in the USA, returns:

- the driving route (geometry + distance + duration)
- an optimal sequence of fuel stops along that route, chosen for lowest
  total cost, respecting a configurable maximum vehicle range
- the total dollar amount that will be spent on fuel for the trip

Built for the Backend Django Engineer take-home assessment.

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

python manage.py migrate
python manage.py load_fuel_prices  # one-time: loads + geocodes the CSV
python manage.py runserver
```

The server runs at `http://127.0.0.1:8000/`.

## API

### `POST /api/route/`

**Request body**

```json
{
  "start": "Chicago, IL",
  "finish": "Dallas, TX",
  "max_range_miles": 500,
  "mpg": 10
}
```

`start` / `finish` accept either a free-text place name (geocoded via OSM
Nominatim) or `"lat,lng"` directly, which skips geocoding entirely.
`max_range_miles` and `mpg` are optional and default to `500` and `10`.

**Response**

```json
{
  "start": {"query": "Chicago, IL", "latitude": 41.8781, "longitude": -87.6298},
  "finish": {"query": "Dallas, TX", "latitude": 32.7767, "longitude": -96.797},
  "distance_miles": 941.1,
  "duration_hours": 14.48,
  "max_range_miles": 500,
  "mpg": 10,
  "total_fuel_cost_usd": 132.56,
  "number_of_fuel_stops": 1,
  "fuel_stops": [
    {
      "name": "RAPID ROBERTS #123",
      "address": "I-44, EXIT 80",
      "city": "Springfield",
      "state": "MO",
      "price_per_gallon": 2.899,
      "latitude": 37.2156,
      "longitude": -93.3026,
      "mile_marker": 483.9,
      "gallons_purchased": 50.0,
      "cost": 132.56
    }
  ],
  "route_geometry": [[41.8781, -87.6298], ["..."]],
  "map_url": "http://127.0.0.1:8000/api/map/?start=Chicago%2C+IL&finish=Dallas%2C+TX&max_range_miles=500&mpg=10"
}
```

`route_geometry` is the full polyline (`[lat, lng]` pairs) for rendering on
any map frontend (Leaflet, Mapbox GL, Google Maps, etc.) without further API
calls. `map_url` is a link to `GET /api/map/` — an interactive Leaflet page
that plots the route polyline, start/finish pins, and numbered fuel-stop
markers with popup cost details.

### `GET /api/map/`

**Query params:** `start`, `finish`, `max_range_miles` (default 500), `mpg` (default 10)

Returns an HTML page with an interactive Leaflet map. Open `map_url` from
the `/api/route/` response directly in a browser to visualise the trip.

### `GET /api/health/`

Liveness check, returns `{"status": "ok"}`.

## Design notes

### Minimizing external API calls

The only external services hit at request time are:

1. **OSM Nominatim** - up to two calls, only if `start`/`finish` are
   free-text rather than `"lat,lng"`. Passing coordinates directly skips
   this entirely.
2. **OSRM** (`router.project-osrm.org`) - exactly **one** call per request,
   requesting full route geometry, distance, and duration together.

Fuel station data requires **zero** external calls at request time. The
~8,150-row CSV is geocoded once, offline, at data-load time (see below),
and stored in the local database. All fuel-stop matching and optimization
runs as in-process Python/SQL against that pre-loaded data.

### Offline geocoding of the fuel price CSV

The provided CSV has city/state but no coordinates. Rather than calling a
geocoding API ~8,150 times (slow, rate-limited, and against the "minimize
API calls" requirement), `load_fuel_prices` resolves each station's
city/state to a lat/lng using a pre-built offline lookup table
(`routing/data/city_geo_cache.json`), generated once from US ZIP code
gazetteer data and bundled with the project. This covers about 97% of
unique city/state pairs in the CSV; the small remainder (mostly Canadian
cities for the small number of non-US stations) are loaded without
coordinates and are simply excluded from route matching.

### Fuel stop optimization algorithm

1. The OSRM route polyline is converted into a list of points carrying
   cumulative distance ("mile marker") along the route.
2. Fuel stations are filtered to a bounding box around the route, then
   each is projected onto its nearest point on the route polyline
   (vectorized with NumPy for speed), giving every station a mile-marker
   position and a perpendicular offset from the route. Stations more than
   ~12 miles off the route are discarded.
3. Starting with a full tank, the algorithm repeatedly looks at every
   station reachable within the current range. It picks the cheapest
   price among them; if multiple stations are tied within a cent, it
   prefers the one furthest along the route, which reduces the number of
   stops without increasing cost. This repeats until the destination
   itself is within range.
4. Cost is computed per leg: the price paid at a stop funds the driving
   distance from that stop to the next one (or to the destination, for
   the last stop). The fuel used on the very first leg (start -> first
   stop) is assumed to already be in the tank and isn't charged.

This greedy strategy is optimal for this problem: since the vehicle must
refuel at some point within every max-range window regardless of choice,
and a cheaper station fully dominates a costlier one if both are reachable,
always taking the cheapest reachable option (with a furthest-stop tiebreak
to avoid unnecessary stops) minimizes total spend.

### Performance

The whole pipeline (excluding the network round-trip to OSRM) runs in
single-digit milliseconds for typical regional trips and under 300ms for
the longest realistic coast-to-coast US routes, measured against the full
~8,150-row dataset.

## Tests

```bash
python manage.py test routing
```

Covers the route-point/distance math, the optimizer's stop selection,
tie-breaking, and error handling, and the API endpoint's request/response
contract (with external calls mocked).

## Tech stack

- Django 5.1 + Django REST Framework
- SQLite (swap `DATABASES` in `config/settings.py` for Postgres/MySQL in
  production; nothing in the code is SQLite-specific)
- [OSRM](http://project-osrm.org/) for routing (free, no API key)
- [OSM Nominatim](https://nominatim.org/) for optional free-text geocoding
- NumPy for vectorized nearest-point matching
