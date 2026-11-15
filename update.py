#!/usr/bin/env python3
"""
Joe's outage tracker - updater.

Runs on a schedule (GitHub Actions). Pulls current outages from each
utility, keeps cleared outages for 24 hours, then deletes them for good.
Writes data/outages.json, which index.html reads.

Python standard library only - nothing to install.
"""

import calendar
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- settings

KEEP_CLEARED_HOURS = 24

# Joe's seven counties. Outages outside these get dropped.
COUNTIES = {"jackson", "madison", "oglethorpe", "greene", "clarke", "oconee", "walton"}

# Rough box around the seven counties, used when a county can't be determined.
BBOX = {"lat_min": 33.40, "lat_max": 34.35, "lon_min": -83.95, "lon_max": -82.85}

# One entry per utility.
#   type "nisc"        = NISC hosted outage map (outagemap.coop). Needs the data slug.
#   type "kubra"       = Kubra StormCenter map. Needs instance + view IDs.
#   type "webviewer"   = Milsoft-style "Web Outage Viewer". Needs the site address.
#   type "outageentry" = OutageEntry/DataVoice map. Needs the client name.
#   type "todo"        = not hooked up; shows as "not connected" on the page.
SOURCES = [
    {"key": "jackson", "name": "Jackson EMC", "type": "nisc", "slug": "jacksonemc"},
    {"key": "walton", "name": "Walton EMC", "type": "outageentry", "client": "walton"},
    {"key": "rayle", "name": "Rayle EMC", "type": "webviewer", "base": "https://outage.rayleemc.com"},
    {"key": "gapower", "name": "Georgia Power", "type": "kubra",
     "instance": "7b38c047-7950-444b-a25c-9b3e5ab986eb", "view": "67b44af5-3847-4ca3-9f4e-9190aac343d6"},
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


def decode_polyline_point(s):
    """First point of a Google-encoded polyline -> (lat, lon)."""
    coords, idx = [], 0
    for _ in range(2):
        shift = result = 0
        while True:
            b = ord(s[idx]) - 63
            idx += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        coords.append(~(result >> 1) if result & 1 else result >> 1)
    return coords[0] / 1e5, coords[1] / 1e5


def tile_bounds(qk):
    """Quadkey -> (lat_min, lat_max, lon_min, lon_max)."""
    x = y = 0
    for ch in qk:
        x, y = x * 2 + (int(ch) & 1), y * 2 + (int(ch) >> 1)
    n = 2 ** len(qk)

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return lat(y + 1), lat(y), x / n * 360 - 180, (x + 1) / n * 360 - 180


def quadkey(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    qk = ""
    for i in range(z, 0, -1):
        mask = 1 << (i - 1)
        qk += str((1 if x & mask else 0) + (2 if y & mask else 0))
    return qk


def tile_hits_bbox(qk):
    s, n, w, e = tile_bounds(qk)
    return not (n < BBOX["lat_min"] or s > BBOX["lat_max"] or e < BBOX["lon_min"] or w > BBOX["lon_max"])


def fetch_kubra(source):
    """Kubra StormCenter (Georgia Power). Walks map tiles over Joe's area,
    zooming into clusters until it reaches individual outages."""
    state = get_json(
        f"https://kubra.io/stormcenter/api/v1/stormcenters/{source['instance']}/views/{source['view']}/currentState?preview=false"
    )
    cluster_path = state["data"]["cluster_interval_generation_data"]
    layer = source.get("layer", "cluster-2")

    def tile(qk):
        url = "https://kubra.io/" + cluster_path.replace("{qkh}", qk[-3:][::-1]) + f"/public/{layer}/{qk}.json"
        try:
            return get_json(url).get("file_data") or []
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return []  # no outages in this tile
            raise

    start = {quadkey(la, lo, 6) for la in (BBOX["lat_min"], BBOX["lat_max"]) for lo in (BBOX["lon_min"], BBOX["lon_max"])}
    todo, found, fetched = list(start), {}, 0
    while todo:
        qk = todo.pop()
        entries = tile(qk)
        fetched += 1
        has_cluster = False
        for ent in entries:
            desc = ent.get("desc") or {}
            pts = (ent.get("geom") or {}).get("p") or []
            if not pts:
                continue
            lat, lon = decode_polyline_point(pts[0])
            if desc.get("cluster"):
                has_cluster = True
                continue
            if not (BBOX["lat_min"] <= lat <= BBOX["lat_max"] and BBOX["lon_min"] <= lon <= BBOX["lon_max"]):
                continue
            oid = desc.get("inc_id") or f"{lat:.4f},{lon:.4f}"
            cust = (desc.get("cust_a") or {}).get("val") or 0
            start_ms = None
            st = desc.get("start_time")
            if isinstance(st, str) and st[:4].isdigit():
                try:
                    start_ms = calendar.timegm(time.strptime(st[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
                except ValueError:
                    start_ms = None
            found[str(oid)] = {
                "id": str(oid), "lat": lat, "lon": lon, "customers": int(cust),
                "start": start_ms, "cause": desc.get("cause"), "county": None,
            }
        if has_cluster and len(qk) < 16 and fetched < 400:
            todo.extend(c for c in (qk + d for d in "0123") if tile_hits_bbox(c))
    return list(found.values())


def parse_time_ms(value):
    """ISO-ish timestamp -> epoch ms, or None. Handles offsets like -05:00."""
    if not isinstance(value, str) or len(value) < 16:
        return None
    import datetime
    v = value.strip().replace("Z", "+00:00")
    if "." in v:  # trim fractional seconds, keep any offset
        head, tail = v.split(".", 1)
        off = ""
        for sign in ("+", "-"):
            if sign in tail:
                off = sign + tail.split(sign, 1)[1]
        v = head + off
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(v, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:  # utility times without an offset are Georgia local time
            dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
            if _is_dst_eastern(dt):
                dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=-4)))
        return int(dt.timestamp() * 1000)
    return None


def _is_dst_eastern(dt):
    """US Eastern DST: 2nd Sunday of March 2am to 1st Sunday of November 2am."""
    import datetime
    y = dt.year
    march = datetime.datetime(y, 3, 8)
    start = march + datetime.timedelta(days=(6 - march.weekday()) % 7, hours=2)
    nov = datetime.datetime(y, 11, 1)
    end = nov + datetime.timedelta(days=(6 - nov.weekday()) % 7, hours=2)
    naive = dt.replace(tzinfo=None)
    return start <= naive < end


def fetch_webviewer(source):
    """Milsoft-style "Web Outage Viewer" (Rayle EMC: outage.rayleemc.com)."""
    rows = get_json(source["base"].rstrip("/") + "/data/outages.json?v=2") or []
    results = []
    for raw in rows:
        pt = raw.get("outagePoint") or {}
        lat, lon = pt.get("lat"), pt.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            lat = lon = None
        cause = (raw.get("cause") or "").strip() or None
        if raw.get("isPlanned"):
            cause = "Planned" + (f" ({cause})" if cause else "")
        oid = raw.get("outageRecID") or raw.get("outageName") or f"{lat},{lon}-{raw.get('outageStartTime')}"
        results.append({
            "id": str(oid), "lat": lat, "lon": lon,
            "customers": int(raw.get("customersOutNow") or 0),
            "start": parse_time_ms(raw.get("outageStartTime")),
            "cause": cause, "county": None,
            "streets": (raw.get("streetsAffected") or None),
        })
    return results


def post_multipart(url, fields):
    boundary = "----outagebot" + str(int(time.time() * 1000))
    body = "".join(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n" for k, v in fields.items()
    ) + f"--{boundary}--\r\n"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers={
        "User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_outageentry(source):
    """OutageEntry / DataVoice map (Walton EMC: outageentry.com)."""
    data = post_multipart("https://www.outageentry.com/Outage/ajax/ajaxShellOut.php", {
        "action": "get", "client": source["client"], "target": "cfa_device_markers",
        "serviceIndex": "1", "port": source.get("port", "8921"),
        "includePrecictions": "false", "includeIndividual": "false", "includeComments": "false",
        "devicesToPolygonize": "[]", "dataUrl": "null",
    })
    if str(data.get("result")).lower() != "true":
        raise ValueError(f"OutageEntry said: {str(data)[:150]}")
    results = []
    for key, block in data.items():
        if not isinstance(block, dict) or "markers" not in block:
            continue
        if str(block.get("index", "1")) != "1":
            continue  # 1 = electric
        for m in block.get("markers") or []:
            try:
                lat, lon = float(m.get("lat")), float(m.get("lon"))
            except (TypeError, ValueError):
                lat = lon = None
            try:
                cust = int(float(m.get("consumers_affected") or 0))
            except (TypeError, ValueError):
                cust = 0
            cause = (m.get("outage_comment") or "").strip() or None
            if m.get("planned") is True:
                cause = "Planned" + (f" ({cause})" if cause else "")
            oid = m.get("incident_id") or m.get("alias") or f"{lat},{lon}-{m.get('start_date')}"
            results.append({
                "id": str(oid), "lat": lat, "lon": lon, "customers": cust,
                "start": parse_time_ms(m.get("start_date")), "cause": cause, "county": None,
            })
    return results


ADAPTERS = {"nisc": fetch_nisc, "kubra": fetch_kubra, "webviewer": fetch_webviewer, "outageentry": fetch_outageentry}

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
                if o.get("streets"):
                    location = f"{o['streets']}" + (f" ({location.rsplit('(', 1)[1]}" if location and "(" in location else "")
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
