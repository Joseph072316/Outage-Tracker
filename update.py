#!/usr/bin/env python3
"""
Joe's outage tracker - updater.

Runs on a schedule (GitHub Actions). Pulls current outages from each
utility, keeps cleared outages for 24 hours, then deletes them for good.
Writes data/outages.json, which index.html reads.

Python standard library only - nothing to install.
"""

import json
import math
import os
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- settings

KEEP_CLEARED_HOURS = 24

# Joe's seven counties. Outages outside these get dropped.
COUNTIES = {"jackson", "madison", "oglethorpe", "greene", "clarke", "oconee", "walton"}

# Rough box around the seven counties, used when a county can't be determined.
BBOX = {"lat_min": 33.40, "lat_max": 34.35, "lon_min": -83.95, "lon_max": -82.85}

# One entry per utility.
#   type "nisc" = NISC hosted outage map (outagemap.coop). Needs the data slug.
#   type "todo" = data address not found yet; shows as "not connected" on the page.
SOURCES = [
    {"key": "jackson", "name": "Jackson EMC", "type": "nisc", "slug": "jacksonemc"},
    {"key": "walton", "name": "Walton EMC", "type": "todo"},
    {"key": "rayle", "name": "Rayle EMC", "type": "todo"},
    {"key": "gapower", "name": "Georgia Power", "type": "todo"},
]

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "outages.json")
USER_AGENT = "joe-outage-tracker/1.0 (personal, non-commercial; github actions)"
TIMEOUT = 30

# ---------------------------------------------------------------- helpers


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def now_ms():
    return int(time.time() * 1000)


def mercator_to_latlon(x, y):
    lon = x / 20037508.34 * 180
    lat = math.degrees(2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2)
    return lat, lon


def decode_rings(rings):
    out = []
    for ring in rings or []:
        if not isinstance(ring, list) or len(ring) < 6 or len(ring) % 2:
            continue
        x, y = ring[0], ring[1]
        pts = [(x, y)]
        for i in range(2, len(ring), 2):
            x += ring[i]
            y += ring[i + 1]
            pts.append((x, y))
        out.append(pts)
    return out


def point_in_rings(px, py, rings):
    inside = False
    for pts in rings:
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def clean_county(name):
    if not name:
        return None
    name = str(name).strip()
    for suffix in (" County", " county", " Co.", " CO"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip().title() or None


def in_territory(county, lat, lon):
    if county:
        return county.lower() in COUNTIES
    if lat is None or lon is None:
        return False
    return BBOX["lat_min"] <= lat <= BBOX["lat_max"] and BBOX["lon_min"] <= lon <= BBOX["lon_max"]


# ---------------------------------------------------------------- adapters
# Each adapter returns a list of dicts:
#   id, lat, lon, customers, start (epoch ms or None), cause, county


def fetch_nisc(source):
    base = f"https://outagemap-data.cloud.coop/{source['slug']}/Hosted_Outage_Map/"
    config = get_json(base + "config.json")
    summary = get_json(base + "summary.json")

    extent = (config.get("mapSettings") or {}).get("boundaryExtent")
    if not (isinstance(extent, list) and len(extent) == 4):
        raise ValueError("config.json has no boundaryExtent")
    xmin, ymin = extent[0], extent[1]

    regions = []
    dataset = (config.get("outageSettings") or {}).get("defaultRegionDataSet")
    if dataset:
        try:
            for feat in get_json(base + f"region.{dataset}.json") or []:
                name = ((feat or {}).get("attributes") or {}).get("name")
                rings = decode_rings(((feat or {}).get("geometry") or {}).get("rings"))
                if name and rings:
                    regions.append((name, rings))
        except Exception as exc:  # regions are a bonus; carry on without them
            print(f"  {source['name']}: regions unavailable ({exc})")

    results = []
    for raw in summary.get("outages") or []:
        x, y = raw.get("x"), raw.get("y")
        lat = lon = county = None
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            mx, my = xmin + x, ymin + y
            lat, lon = mercator_to_latlon(mx, my)
            for name, rings in regions:
                if point_in_rings(mx, my, rings):
                    county = clean_county(name)
                    break
        try:
            customers = int(raw.get("nbrOut") or 0)
        except (TypeError, ValueError):
            customers = 0
        cause = (raw.get("cause") or "").strip() or None
        if raw.get("planned") is True:
            cause = "Planned" + (f" ({cause})" if cause else "")
        oid = raw.get("id")
        if oid is None:
            oid = f"{round(lat or 0, 4)},{round(lon or 0, 4)}-{raw.get('timeOff')}"
        results.append({
            "id": str(oid),
            "lat": lat,
            "lon": lon,
            "customers": customers,
            "start": raw.get("timeOff") if isinstance(raw.get("timeOff"), (int, float)) else None,
            "cause": cause,
            "county": county,
        })
    return results


ADAPTERS = {"nisc": fetch_nisc}

# ---------------------------------------------------------------- location text

_last_geocode = [0.0]


def reverse_geocode(lat, lon):
    """Road name + town + zip from OpenStreetMap. One request per new outage."""
    wait = 1.1 - (time.time() - _last_geocode[0])  # OSM asks for max 1 request/second
    if wait > 0:
        time.sleep(wait)
    _last_geocode[0] = time.time()
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 17, "addressdetails": 1}
    )
    try:
        data = get_json(url)
    except Exception as exc:
        print(f"  geocode failed ({exc})")
        return None, None
    addr = data.get("address") or {}
    road = addr.get("road")
    town = addr.get("village") or addr.get("town") or addr.get("city") or addr.get("hamlet") or addr.get("suburb")
    zipcode = addr.get("postcode")
    county = clean_county(addr.get("county"))
    if road and town:
        text = f"{road} near {town}"
    elif road:
        text = road
    elif town:
        text = f"near {town}"
    else:
        text = None
    if zipcode:
        text = f"{text} ({zipcode})" if text else f"ZIP {zipcode}"
    return text, county


# ---------------------------------------------------------------- main


def load_existing():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"outages": [], "sources": {}}


def main():
    t = now_ms()
    data = load_existing()
    by_key = {o["key"]: o for o in data.get("outages", [])}
    sources_status = data.get("sources", {})
    skipped = {k: v for k, v in (data.get("skipped") or {}).items() if v > t - 24 * 3600 * 1000}

    for src in SOURCES:
        status = sources_status.get(src["key"], {})
        status["name"] = src["name"]
        adapter = ADAPTERS.get(src["type"])
        if adapter is None:
            status.update({"state": "not_connected", "message": "Data source not hooked up yet"})
            sources_status[src["key"]] = status
            continue

        try:
            current = adapter(src)
        except Exception as exc:
            # Fetch failed: leave this utility's outages exactly as they were.
            print(f"{src['name']}: FAILED - {exc}")
            status.update({"state": "error", "message": str(exc)[:200], "last_error": t})
            sources_status[src["key"]] = status
            continue

        seen = set()
        kept = 0
        for o in current:
            key = f"{src['key']}:{o['id']}"
            existing = by_key.get(key)
            if existing is None:
                if key in skipped or not in_territory(o["county"], o["lat"], o["lon"]):
                    continue  # outside Joe's area
                location, geo_county = (None, None)
                if o["lat"] is not None:
                    location, geo_county = reverse_geocode(o["lat"], o["lon"])
                county = o["county"] or geo_county
                if not in_territory(county, o["lat"], o["lon"]) or (county and county.lower() not in COUNTIES):
                    skipped[key] = t  # remember so it isn't looked up again
                    continue
                existing = {
                    "key": key,
                    "utility": src["name"],
                    "lat": o["lat"],
                    "lon": o["lon"],
                    "county": county or "Unknown",
                    "location": location,
                    "first_seen": t,
                }
                by_key[key] = existing
            existing.update({
                "customers": o["customers"],
                "start": o["start"] or existing.get("start") or existing["first_seen"],
                "cause": o["cause"],
                "last_seen": t,
                "cleared": None,
            })
            seen.add(key)
            kept += 1

        # Anything from this utility we had as active but didn't see now has cleared.
        for key, o in by_key.items():
            if key.startswith(src["key"] + ":") and key not in seen and not o.get("cleared"):
                o["cleared"] = t

        status.update({"state": "ok", "message": f"{kept} outage(s) in your counties", "last_ok": t})
        sources_status[src["key"]] = status
        print(f"{src['name']}: ok, {len(current)} total, {kept} in territory")

    # Delete cleared outages older than 24 hours - completely, no record.
    cutoff = t - KEEP_CLEARED_HOURS * 3600 * 1000
    outages = [o for o in by_key.values() if not (o.get("cleared") and o["cleared"] < cutoff)]
    outages.sort(key=lambda o: (o.get("cleared") is not None, -(o.get("start") or 0)))

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump({"checked": t, "outages": outages, "sources": sources_status, "skipped": skipped}, f, indent=1)


if __name__ == "__main__":
    main()
