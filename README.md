# Fuel Route Planner API

A Django REST API that takes a start and a finish in the contiguous United States and returns

* the **driving route** (GeoJSON geometry, ready for a map),
* the **cheapest set of fuel stops** along it for a vehicle with a **500-mile range** at **10 mpg**,
* the fuel bought, money spent per stop, and the **total fuel cost** in USD.

It uses the supplied fuel-price CSV, one free routing request per new journey (OSRM), and an
optimiser whose correctness is checked against independent exact solvers (see
[How the optimiser works](#how-the-optimiser-works)).

> **Quick look:** [`docs/examples/`](docs/examples) holds real responses of this code
> (with made-up stations, see [note](#about-the-example-files)), and
> [`postman/`](postman) holds a ready-to-import Postman collection.

---

## Contents

1. [Quick start (Windows)](#quick-start-windows)
2. [Configuration](#configuration)
3. [The dataset](#the-dataset)
4. [Using the API](#using-the-api)
5. [API reference](#api-reference)
6. [How the optimiser works](#how-the-optimiser-works)
7. [Design decisions and assumptions](#design-decisions-and-assumptions)
8. [Known limitations](#known-limitations)
9. [Tests, quality and performance](#tests-quality-and-performance)
10. [Project layout](#project-layout)
11. [Reproducing the demonstration](#reproducing-the-demonstration)
12. [Troubleshooting](#troubleshooting)
13. [Data sources and attribution](#data-sources-and-attribution)

---

## Quick start (Windows)

Requirements: **Python 3.12 or newer** (developed on 3.13; Django 6.1 does not run on 3.11) and
Git. No API keys are needed.

```powershell
# 1. Create and activate a virtual environment
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked:  Set-ExecutionPolicy -Scope Process Bypass

# 2. Install dependencies (requirements-dev.txt = runtime + test tools)
pip install -r requirements-dev.txt

# 3. Configure
copy .env.example .env                # the defaults work for local development

# 4. Put the assessment CSV in place (it is NOT part of this repository, see "The dataset")
copy "C:\path\to\fuel-prices-for-be-assessment.csv" data\

# 5. Create the database and import the stations
python manage.py migrate
python manage.py load_fuel_data

# 6. Run the server (loads all data first, so the first request is fast)
python manage.py runserver
```

Check it is alive: <http://127.0.0.1:8000/api/v1/health/> should report
`"status": "ok"` with about 6,600 stations.

Run the tests: `pytest` (see [Tests](#tests-quality-and-performance)).

On macOS/Linux the same steps work with `python3 -m venv .venv`, `source .venv/bin/activate`
and `cp` instead of `copy`.

---

## Configuration

Settings come from environment variables, optionally loaded from a local `.env` file
(git-ignored; copy [`.env.example`](.env.example)). Nothing secret is stored in the repository.

| Variable | Default | Meaning |
|---|---|---|
| `DJANGO_DEBUG` | `false` | `true` for local development (also enables the browsable API). |
| `DJANGO_SECRET_KEY` | – | **Required when `DJANGO_DEBUG` is false**; the app refuses to start without it. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],testserver` | Comma-separated host names. |
| `FUEL_CSV_PATH` | `data/fuel-prices-for-be-assessment.csv` | Where the assessment CSV is. |
| `GAZETTEER_PATH` | `data/us_places.csv.gz` | Place-name → coordinates table (shipped, see below). |
| `ROUTING_PROVIDER` | `osrm` | `osrm`, or a dotted path to your own `RoutingProvider` class. |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Point at a self-hosted OSRM for real traffic. |
| `OSRM_PROFILE` | `driving` | OSRM profile. |
| `OSRM_TIMEOUT_SECONDS` | `15` | Timeout for the routing request. |
| `OSRM_MIN_INTERVAL_SECONDS` | `1.0` | Minimum gap between outgoing routing requests (public server limit). |
| `HTTP_USER_AGENT` | `spotter-fuel-route-assessment/1.0 …` | Identifies you to OSRM and the Census geocoder (their policies require it). Put your contact address in it. |
| `ROUTE_CACHE_SECONDS` / `GEOCODE_CACHE_SECONDS` | 1 day / 30 days | Cache lifetimes. |
| `CACHE_MAX_ENTRIES` | `200` | Cap on the in-process cache (a coast-to-coast route is about 0.5 MB). |
| `THROTTLE_RATE` | `120/min` | API rate limit per client IP. |
| `LOG_LEVEL` | `INFO` | Logging level of the `fuelplanner` logger. |
| `DATABASE_FILE` | `db.sqlite3` | SQLite file name. |

Production notes: set `DJANGO_DEBUG=false`, a real `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS`;
run behind a real WSGI server (`config.wsgi.application`); use a shared cache (Redis/Memcached)
if you run several processes; and use your own OSRM server instead of the public demo, which is
best-effort, limited to one request per second, and not meant for production traffic.

---

## The dataset

**The assessment CSV is not included in this repository.** Its redistribution rights are not
established, so `data/*.csv` is git-ignored. Place your copy at
`data/fuel-prices-for-be-assessment.csv` (or set `FUEL_CSV_PATH`). If it is missing, the import
command stops with an explanatory message, and tests that need it skip themselves.

The CSV has 8,151 rows: `OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail
Price`. **It has no coordinates.** Addresses look like `I-44, EXIT 283 & US-69`, which no ordinary
geocoder can resolve, and geocoding thousands of stations through an external service would break
the "minimal API calls" goal and the providers' usage policies. So stations are located offline:

1. [`data/us_places.csv.gz`](data/us_places.csv.gz) (committed, 3.9 MB, public-domain
   U.S. government data) maps `City, State` → coordinates. It merges the Census Bureau Gazetteer
   (places, county subdivisions, ZIP areas) and USGS GNIS populated places. Rebuild it with
   `python manage.py build_gazetteer` (downloads the source files).
2. `python manage.py load_fuel_data` cleans the CSV, places each station at the **centroid of its
   city**, and writes `data/import_report.json` (git-ignored).

What the import does with the supplied file (all reported when you run it):

| Step | Result |
|---|---|
| Rows read | 8,151 |
| Canadian rows excluded (AB, BC, ON, …) | 620 |
| Rows rejected as malformed / implausible | 0 |
| Exact duplicate rows | 26 |
| US stations (unique OPIS IDs) | 6,626 |
| Stations whose rows disagree on price | 487 – the **lowest** price is kept |
| Stations located | **6,614 (99.82 %)** |
| Stations not located (excluded, never guessed) | 12 – Ottawa Lake MI, Etters PA, Pueblo of Acoma NM, Feasterville Trevose PA, Ellenwood GA, Rabun Gap GA, Willow Beach AZ |

**Accuracy of station positions.** A city centroid is not the pump. Checked on the 189 stations
whose address names I-80 along a New York → Los Angeles route, the median distance from the
centroid to the actual road was 1.6 miles (90 % within 4.5 miles). The API labels every station
`"location_precision": "city"` and `"approximate_location": true`, and the response lists this
among its `assumptions`. The route itself and the start/finish are precise when you give
coordinates or an address.

The import is re-runnable (`load_fuel_data` replaces the table in one transaction; add
`--dry-run` to only analyse). Restart the server after re-importing.

---

## Using the API

### With Postman

Import [`postman/fuel-route.postman_collection.json`](postman/fuel-route.postman_collection.json).
It has a `base_url` variable (`http://127.0.0.1:8000`), requests for every feature and every error
case, automatic test assertions, and a **Visualize** tab on the long trip that draws the route and
the stops on a map. Run **Health check** first.

### With PowerShell

```powershell
$body = @{ start = "Chicago, IL"; finish = "Dallas, TX" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/fuel-route/ `
  -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8
```

### With curl

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/fuel-route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX", "options": {"geometry": "none"}}'
```

### Seeing the route on a map

Request `?format=geojson`, save the output, and drop it on <https://geojson.io>: the route,
start, finish and numbered stops appear. No separate front end is needed: the JSON response
already carries the geometry and the stop coordinates (see below).

---

## API reference

### `POST /api/v1/fuel-route/`

Request body (JSON):

```json
{
  "start":  "Chicago, IL",
  "finish": "Dallas, TX",
  "options": { "max_detour_miles": 5, "initial_fuel_fraction": 1.0, "geometry": "simplified" }
}
```

**Locations** (`start`, `finish`) – any of:

| Form | Example | Resolved by | `precision` |
|---|---|---|---|
| City and state | `"Chicago, IL"`, `"chicago, illinois"`, `"Saint Louis, MO"` | offline place table | `city` |
| ZIP code | `"60601"`, `"60601-1234"` | offline ZIP table | `zip_centroid` |
| Coordinates | `{"lat": 41.88, "lon": -87.63}` or `"41.88,-87.63"` | as given | `exact` |
| Street address (with house number) | `"233 S Wacker Dr, Chicago, IL 60606"` | Census Geocoder (1 call, cached) | `address` |

Only the contiguous United States is supported.

**Options** (all optional; unknown keys are rejected):

| Option | Range / default | Meaning |
|---|---|---|
| `max_detour_miles` | 0.5–25, default 5 | Stations farther than this (straight line) from the route are ignored. |
| `initial_fuel_fraction` | 0–1, default 1 | Fuel in the tank at the start, as a fraction of a full tank. |
| `geometry` | `simplified` (default), `full`, `none` | Route geometry detail in the response. |

`?format=geojson` returns a GeoJSON `FeatureCollection` (route line, start, finish, numbered
stops) instead of the JSON below.

**Response `200`** (abridged; full files in [`docs/examples/`](docs/examples)):

```jsonc
{
  "start":  { "input": "Chicago, IL", "label": "Chicago, IL", "lat": 41.83705, "lon": -87.68494,
              "resolved_by": "gazetteer_city", "precision": "city" },
  "finish": { "...": "same shape" },
  "route": {
    "distance_miles": 961.5, "duration_hours": 17.04,
    "geometry_mode": "simplified", "geometry_points": 741,
    "geometry": { "type": "LineString", "coordinates": [[-87.68494, 41.83705], "..."] }   // [lon, lat]
  },
  "vehicle": { "max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_gallons": 50.0,
               "initial_fuel_gallons": 50.0 },
  "fuel_stops": [
    {
      "sequence": 1,
      "station": { "opis_id": 900002, "name": "…", "address": "…", "city": "Marion", "state": "IL",
                   "lat": 37.734631, "lon": -88.944227,
                   "location_precision": "city", "approximate_location": true },
      "price_per_gallon": 3.019,
      "route_mile": 313.0,                 // distance along the route to the stop
      "off_route_miles": 0.8, "detour_miles": 1.6,
      "distance_from_previous_miles": 313.8,
      "fuel_on_arrival_gallons": 18.62, "gallons_purchased": 30.19,
      "fuel_on_departure_gallons": 48.81,
      "cost": 91.14
    }
  ],
  "legs":    [ { "sequence": 1, "from": "start", "to": "stop 1",
                 "distance_miles": 313.8, "fuel_used_gallons": 31.38 }, "..." ],
  "summary": { "fuel_stops": 2, "route_distance_miles": 961.5, "driving_distance_miles": 963.7,
               "detour_miles": 2.2, "fuel_used_gallons": 96.37, "initial_fuel_gallons": 50.0,
               "fuel_purchased_gallons": 46.37, "fuel_remaining_at_destination_gallons": 0.0,
               "total_fuel_cost": 137.23, "average_price_per_gallon": 2.96, "currency": "USD" },
  "assumptions": [ "…plain-language list of every assumption behind the numbers…" ],
  "meta": { "routing_provider": "osrm", "routing_cache": "miss",
            "external_calls": { "routing": 1, "geocoding": 0 },
            "candidate_stations": 7, "max_detour_miles": 5.0, "elapsed_ms": 31,
            "attribution": "Routing by OSRM … © OpenStreetMap contributors (ODbL) …" }
}
```

Units and rounding (display only; arithmetic is exact): miles 1 decimal, gallons 2, price per
gallon 3, money 2 (half-up), coordinates 6 (5 along the route). Each stop's `cost` is gallons ×
the full-precision price rounded to the cent, and `total_fuel_cost` is the sum of those lines.
Fuel balances: `initial + purchased − used = remaining` (to display rounding).

**Using the response on a map.** `route.geometry` is a standard GeoJSON `LineString` in
`[lon, lat]` order and every stop has `lat`/`lon`, so any map library can draw it; with
`?format=geojson` the same data arrives as a ready-made `FeatureCollection`.

**Errors.** Every failure has the same shape and a stable `code`:

```json
{ "error": { "code": "no_feasible_fuel_plan", "message": "…", "details": { } } }
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Missing/invalid field, unknown key, bad option (`details` lists them). |
| 400 | `invalid_json` | Body is not valid JSON. |
| 400 | `same_location` | Start and finish are the same place. |
| 422 | `location_not_found` | The text matches no city/ZIP/address. |
| 422 | `location_outside_usa` | Outside the contiguous United States. |
| 422 | `route_not_found` | No driving route between the points. |
| 422 | `no_feasible_fuel_plan` | The destination cannot be reached with the data and range. `details` names the gap: `last_reachable_route_mile`, `distance_to_next_option_miles`, `shortfall_miles`, and `gap_start` / `gap_end` coordinates. |
| 429 | `rate_limited` | Too many requests (`Retry-After` header). |
| 404 / 405 / 406 / 415 | `not_found`, `method_not_allowed`, `not_acceptable`, `unsupported_media_type` | Wrong URL, method, `Accept` or content type. |
| 500 | `internal_error`, `plan_invariant_violation` | A bug; details are logged, never returned. |
| 502 | `routing_provider_error`, `geocoding_provider_error` | Upstream service answered badly. |
| 503 | `routing_provider_unavailable` | Router down or rate limiting (`Retry-After` when given). |
| 503 | `fuel_data_unavailable`, `location_data_unavailable` | Station table empty / database not ready, or place table missing. |
| 504 | `routing_provider_timeout` | Router did not answer in time. |

### `GET /api/v1/health/`

Returns `200` with `"status": "ok"`, the number of stations and places loaded, the routing
provider and Django version; `503` with a hint if no stations are loaded. It also warms the
caches, so call it before a demo.

---

## How the optimiser works

**Model.** Driving burns 1 gallon per 10 miles; the tank holds 50 gallons (500 miles). A
candidate station sits at some *route mile* and some distance *off the route*. Visiting it means
driving out and back, and that extra fuel is paid for. Fuel can be bought in any amount, and the
vehicle may arrive empty. The goal is the **minimum total fuel cost**, never running dry.

**Pipeline.** Resolve both locations → one OSRM request for the full route → resample the route
every 0.5 mile → find stations within the detour limit with a KD-tree (each gets a route mile and
an off-route distance) → optimise → replay the plan leg by leg in exact `Decimal` arithmetic.

**Algorithm.** A dynamic programme over the candidate stations in route order. Two facts shrink
the search:

1. *Stopping without buying never helps* – skipping a station never lengthens the trip.
2. *Between two buying stops an optimal plan either fills the tank or buys just enough to
   arrive at the next stop empty.* Otherwise purchases can be shifted toward the cheaper station
   without raising the cost.

So fuel on arrival is one of three values: empty, "full tank minus the leg", or "starting fuel
minus the leg". The DP keeps the cheapest way to reach each station in each state, and tries both
moves from each. Complexity is about O(n·m·log m) for n candidates with m inside one tank range;
600 candidates over 6,000 miles take roughly 75 ms.

**Worked example** – a 1,100-mile route, full tank at the start (500 miles of range):

| Station | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| Route mile | 150 | 420 | 480 | 700 | 850 | 980 |
| $/gal | 3.80 | 3.40 | 3.10 | 3.90 | 2.95 | 3.60 |

E is cheapest but 850 miles away, beyond the first tank, so it cannot be a first stop. A and B
are skipped because C is cheaper and reachable. At C (480 mi) 2 gallons remain; E is cheaper and
370 miles on, so the plan buys *just enough* to get there: 35 gal × $3.10 = **$108.50**. At E
the tank is empty and the destination is 250 miles away: 25 gal × $2.95 = **$73.75**.
Total **$182.25**, two stops, 60 gallons (50 on board + 60 bought = 110 gal for 1,100 miles).
This case is a unit test.

**Why it is trustworthy.** "Cheapest station" or "fill up whenever low" strategies fail on cases
like the one above. This optimiser is verified (see [Tests](#tests-quality-and-performance)):
it matches an exhaustive search over every subset of stops on 960 random small instances, matches
a second, independent exact solver on 190 realistic ones (differences ~1e-13 dollars), satisfies
14 property tests ("adding a station never raises the cost", …), and never costs more than
three common driver strategies. Under the model above it is exact for the candidate stations
given to it; the model's approximations are listed below.

---

## Design decisions and assumptions

* **Starting fuel.** The vehicle starts with a full tank (adjustable with
  `initial_fuel_fraction`) and that fuel is *not* counted as a purchase – the brief does not say
  otherwise. Consequence: any trip within 500 miles costs **$0.00**. The response says so in
  `assumptions`. To price the starting fuel as well, add it to the result yourself or choose a
  smaller `initial_fuel_fraction` with a station at the start.
* **Prices.** Each station uses the lowest price listed for it; prices are treated as current
  USD per gallon at an accessible stop. The CSV has no fuel type, hours or timestamps.
* **Routing.** OSRM, because it is free, needs no key, and returns the whole route geometry in one
  request. Identical journeys are served from a cache; simultaneous identical requests are
  collapsed into one. The provider is replaceable through configuration.
* **No per-station geocoding.** See [The dataset](#the-dataset).
* **Detours.** A station is only considered within `max_detour_miles` (straight line) of the
  route, and the out-and-back trip is charged as driven miles and fuel. This is conservative: a
  real exit ramp is usually shorter than a straight-line out-and-back.
* **Honesty over plausibility.** Nothing is invented: unlocated stations are left out, malformed
  CSV rows are rejected rather than repaired, and an impossible trip returns an explicit error
  with the gap, not a made-up plan.

---

## Known limitations

* **Station positions are city-level** (typically within a few miles). A station can be missed or
  wrongly included near the corridor edge; use a larger `max_detour_miles` to be safe.
* **Coverage gaps change the answer.** The data has only a few dozen stations in California and Oregon combined, so
  some West Coast trips (for example Seattle → San Diego) are genuinely infeasible with a
  500-mile range and the error says where.
* **The public OSRM server** is best-effort, throttled to one request per second, and not for
  production traffic. Cold requests take about a second, mostly waiting for it. Use your own OSRM
  for real use.
* **Coordinates close to the border** (for example in Canada) can be accepted because the check is
  a bounding box plus "near a known U.S. place", not a true border test. City, ZIP and address
  inputs are always U.S.
* **Contiguous US only.** No Alaska, Hawaii or territories.
* **No traffic, tolls, truck restrictions or opening hours;** durations are OSRM's free-flow
  estimates. Prices are a snapshot.
* **Single-process cache.** The default cache lives in memory; use Redis/Memcached when running
  several workers. Restart after re-importing stations.
* **Vehicle range and mpg are fixed at 500 / 10** by the brief (configurable in code
  settings, not per request).

---

## Tests, quality and performance

```powershell
pytest                                   # 454 tests, no network access needed (about 40 s)
pytest --cov --cov-fail-under=95         # with coverage: 99.7 % of lines
pytest -m live                           # 2 smoke tests against the real OSRM / Census services
ruff check . ; ruff format --check .     # lint and formatting
```

Tests that need the real CSV skip themselves when it is absent; everything else runs against
small synthetic fixtures, so a fresh clone passes (verified in a clean environment). A GitHub
Actions workflow ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, Django checks
and the suite on every push.

What is covered: the optimiser (exhaustive and reference-solver comparison, property tests,
boundary cases, infeasible routes), fuel/cost arithmetic and reconciliation, station matching and
route geometry, location resolution, the OSRM client (every failure mode, throttling, caching,
single-flight), the API contract and every error response, the importer on messy files, the
shipped place table, whole-pipeline randomised invariants, golden tests on the real dataset with
recorded routes, concurrency, and performance budgets. To check that the tests can actually fail,
39 deliberately injected bugs were each caught.

Measured on a laptop (Python 3.13, your CSV):

| What | Time |
|---|---|
| Server start-up warm-up (place table + stations) | ≈ 0.8 s, once |
| Planning a coast-to-coast trip, router mocked | ≈ 27 ms |
| Repeat of a cached trip, end to end | ≈ 25–55 ms |
| New trip, live OSRM (NYC → LA) | ≈ 1.0–1.2 s (mostly OSRM) |
| Optimiser: 600 candidates, 6,000 miles | ≈ 75 ms |
| Response size, NYC → LA | 49 KB (75 KB with `geometry: full`, 6 KB with `none`) |

Every response reports `meta.external_calls` and `meta.elapsed_ms`. A new journey makes exactly
**one** routing request (plus one geocoding request only for a street address); an identical
repeat makes **none**.

---

## Project layout

```
config/                 Django project (settings, urls, wsgi; settings_test.py for the tests)
fuelplanner/
  api/                  views, serializers, response builder, error handling, urls
  services/
    locations.py        text / ZIP / coordinates / address  ->  coordinates
    geocoding.py        Census Geocoder client (street addresses only)
    routing.py          OSRM client, throttle, cache, replaceable provider
    geometry.py         distances, resampling, route simplification
    stations.py         station index and route-corridor matching
    optimizer.py        the dynamic programme
    accounting.py       exact fuel & money ledger with self-checks
    planner.py          puts the steps together
    gazetteer.py, text.py, states.py, warmup.py, callcount.py
  importer.py           CSV cleaning and station placement
  management/commands/  build_gazetteer, load_fuel_data, runserver (warms up first)
  models.py             Station
data/us_places.csv.gz   shipped place table (the CSV itself is git-ignored)
docs/examples/          example requests' responses
postman/                Postman collection
tests/                  test-suite, fixtures (synthetic data, recorded OSRM routes)
```

---

## Reproducing the demonstration

1. Follow the [Quick start](#quick-start-windows); start the server and open **Health check** in
   Postman (it warms everything).
2. Run the collection's requests in order: short trip ($0, no stops) → Chicago → Dallas (two
   stops) → New York → Los Angeles (eight stops; open **Visualize** for the map) → the same
   request again (note `routing_cache: "hit"`, `external_calls: 0`) → a wider detour →
   half-full start → Seattle → San Diego (the explicit "no feasible plan" error) → a
   validation error.
3. For a map of any result, send it with `?format=geojson` and paste the output into geojson.io.

Expected numbers with the supplied dataset, live routing: Chicago → Dallas ≈ $134, New York →
Los Angeles ≈ $713 (8 stops, longest leg about 462 miles). Small differences are normal if OSM
road data changes.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ImproperlyConfigured: DJANGO_SECRET_KEY is not set` | `copy .env.example .env` (or set `DJANGO_DEBUG=true`). |
| `Fuel price CSV not found` | Put the CSV in `data\` or set `FUEL_CSV_PATH`. |
| Health says `unavailable` / `fuel_data_unavailable` | Run `python manage.py migrate` then `python manage.py load_fuel_data`. |
| `routing_provider_unavailable` / 429 | The public OSRM is busy or rate limiting; wait and retry (repeat requests are cached). |
| `no_feasible_fuel_plan` | Genuine data gap on that route; try a larger `max_detour_miles` or check `details.gap_start`. |
| Activate.ps1 is blocked | `Set-ExecutionPolicy -Scope Process Bypass` in that window. |
| `No matching distribution` for Django 6.1 | You are on Python < 3.12; install Python 3.13. |

---

## Data sources and attribution

* Routing: [OSRM](https://project-osrm.org/); map data © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, ODbL. Recorded routes in `tests/fixtures/osrm` are from the public OSRM demo server.
* Place coordinates: U.S. Census Bureau Gazetteer files and USGS Geographic Names Information
  System (U.S. public domain).
* Street-address geocoding: U.S. Census Bureau Geocoder.
* Fuel prices: the OPIS dataset supplied with the assessment (not redistributed here).

### About the example files

`docs/examples/` uses **synthetic, clearly named example stations**
("EXAMPLE TRAVEL CENTER #n") with invented prices, produced by running this code on recorded
routes. They show the format and the arithmetic, not real stations. Real results come from your
copy of the CSV.
