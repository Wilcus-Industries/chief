---
name: maps
description: Geocode places, find nearby POIs, get routes/travel times, and look up timezones with the bundled maps_client.py (OpenStreetMap/OSRM via the shell tool) — no API key.
---

# maps — location answers via OpenStreetMap

`skills/maps/scripts/maps_client.py` (bundled by this package, stdlib-only)
talks to Nominatim, Overpass, OSRM, and TimeAPI.io. Run it through the
**shell tool** from the repo root:

```
MAPS=skills/maps/scripts/maps_client.py
```

## Commands

```
uv run python $MAPS search "Eiffel Tower"                 # place → lat/lon
uv run python $MAPS reverse 48.8584 2.2945                # lat/lon → address
uv run python $MAPS nearby --near "Times Square" --category cafe --limit 5
uv run python $MAPS nearby 37.77 -122.42 pharmacy --radius 2000
uv run python $MAPS distance "Home address" --to "SFO" --mode driving
uv run python $MAPS directions "Ferry Building" --to "Oracle Park" --mode walking
uv run python $MAPS timezone 35.6762 139.6503
uv run python $MAPS area "Manhattan" ; uv run python $MAPS bbox S W N E restaurant
```

- `nearby` needs lat/lon **or** `--near "<address/landmark/zip>"`; ~46
  categories (restaurant, cafe, pharmacy, hospital, gas_station, hotel,
  atm, supermarket, …). Results carry `name`, `distance_m`, `maps_url`
  (tap-to-open Google Maps link), and sometimes `hours`/`phone`/`cuisine`.
- `distance`/`directions` take the destination via `--to`, modes driving
  (default) / walking / cycling.

## Presenting to the owner

Numbered list, name + distance + the `maps_url` link (tappable in iMessage).
For "is it open?" treat OSM `hours` as a hint, not truth — community data
goes stale; verify with a web search when it matters.

## Rules

- Nominatim allows 1 req/s — the script rate-limits itself; don't parallelize
  around it.
- Ambiguous zip/place? Add city/state/country to the query.
- OSRM coverage is best in North America + Europe; say so if routing looks
  off elsewhere.
- If the script errors on all mirrors (Overpass peaks), wait and retry once
  before reporting failure.
