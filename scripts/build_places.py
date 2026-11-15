"""Build app/data/places.jsonl (one place a line, so a refresh is a readable diff), the searchable list of towns and
neighbourhoods (app/services/places.py). Run by hand when the list is
refreshed; the server only reads the result.

Sources (both free with credit):
- GeoNames towns of 1,000+ people (via the all-the-cities package, CC BY 4.0):
  every town in Africa; outside Africa only places of 15,000+ people.
- OpenStreetMap places (ODbL, "© OpenStreetMap contributors"), downloaded per
  country with Overpass: city, town, suburb, neighbourhood, quarter. These give
  the neighbourhoods (Ntinda, Kololo, Yaba, Westlands...).

Ids stay the same between builds ("o" + OSM node id, "g" + GeoNames id), so an
area someone picked keeps working after a refresh.

    python scripts/build_places.py geonames.json osm/*.json > /dev/null
"""

import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data.home_ground_cities import CITY_LIST, COUNTRY_NAMES  # noqa: E402
from app.services.home_ground import _fold, slug  # noqa: E402

AFRICA = set("dz ao bj bw bf bi cv cm cf td km cg cd ci dj eg gq er sz et ga gm gh gn gw ke ls lr ly mg mw ml mr "
             "mu ma mz na ne ng rw st sn sc sl so za ss sd tz tg tn ug zm zw".split())
OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data", "places.jsonl")
# Home Ground names that GeoNames spells differently.
OTHER_NAMES = {"Huye": "Butare", "Rubavu": "Gisenyi", "Mekelle": "Mek'ele", "Hargeisa": "Hargeysa",
               "Serrekunda": "Serekunda", "Gqeberha": "Port Elizabeth", "Marrakech": "Marrakesh",
               "New York": "New York City", "Washington DC": "Washington, D.C.", "Quebec City": "Québec",
               "Antwerp": "Antwerpen", "Frankfurt": "Frankfurt am Main", "Cologne": "Köln", "Geneva": "Genève",
               "Panama City": "Panamá", "Cap-Haïtien": "Okap"}
AREA_TO_CITY_KM = 25      # a neighbourhood belongs to the nearest Home Ground city this close
SAME_PLACE_KM = {"area": 1.5, "town": 6.0, "city": 15.0}


def km(a, b):
    r = 6371.0
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def from_geonames(path):
    out = []
    for gid, name, cc, code, pop, lat, lng in json.load(open(path)):
        cc = (cc or "").lower()
        if cc not in COUNTRY_NAMES or not name:
            continue
        pop = int(pop or 0)
        if cc not in AFRICA and pop < 15000 and code not in ("PPLC", "PPLA"):
            continue
        if code == "PPLX":
            kind = "area"
        elif code in ("PPLC", "PPLA") or pop >= 100000:
            kind = "city"
        else:
            kind = "town"
        out.append({"id": f"g{gid}", "name": name.strip(), "kind": kind, "country": cc, "lat": round(lat, 5),
                    "lng": round(lng, 5), "pop": pop, "src": "g"})
    return out


OSM_KIND = {"city": "city", "town": "town", "suburb": "area", "neighbourhood": "area", "quarter": "area"}


BAD_WORDS = ("slum", "supposed", "plantation", "owners", "http", "unnamed", "test")


class Nearest:
    """The country of the nearest GeoNames town (OSM downloads are boxes that
    cross borders)."""

    def __init__(self, geonames_path):
        self.grid = defaultdict(list)
        for gid, name, cc, code, pop, lat, lng in json.load(open(geonames_path)):
            self.grid[(int(lat // 1), int(lng // 1))].append((lat, lng, (cc or "").lower()))

    def country(self, lat, lng):
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for la, lo, cc in self.grid.get((int(lat // 1) + dy, int(lng // 1) + dx), []):
                    d = km((lat, lng), (la, lo))
                    if best is None or d < best[0]:
                        best = (d, cc)
        return best[1] if best and best[0] <= 60 else None


def from_osm(path, nearest):
    out = []
    for e in json.load(open(path)):
        kind = OSM_KIND.get(e.get("place"))
        name = " ".join((e.get("name") or e.get("en") or "").split())
        if not kind or not name or len(name) > 40 or len(name.split()) > 5 or any(ch.isdigit() for ch in name):
            continue
        if any(w in name.casefold() for w in BAD_WORDS):
            continue
        # Mostly Latin names only for now (search is typed in Latin letters).
        if sum(ord(ch) > 0x24F for ch in name) > len(name) // 3:
            name = " ".join((e.get("en") or "").split())
            if not name or sum(ord(ch) > 0x24F for ch in name) > len(name) // 3:
                continue
        cc = nearest.country(e["lat"], e["lon"])
        if cc not in COUNTRY_NAMES:
            continue
        try:
            pop = int(str(e.get("pop") or "0").replace(",", "").split(".")[0] or 0)
        except ValueError:
            pop = 0
        out.append({"id": f"o{e['id']}", "name": name, "kind": kind, "country": cc, "lat": round(e["lat"], 5),
                    "lng": round(e["lon"], 5), "pop": pop, "src": "o"})
    return out


def dedupe(rows):
    """The same place from both sources (or twice in one): keep one, OSM first
    for areas, the bigger population for towns and cities."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["country"], _fold(r["name"]))].append(r)
    kept = []
    for _, rs in groups.items():
        rs.sort(key=lambda r: (r["kind"] != "area" or r["src"] != "o", -r["pop"], r["id"]))
        mine = []
        for r in rs:
            twin = next((m for m in mine if km((m["lat"], m["lng"]), (r["lat"], r["lng"]))
                         <= max(SAME_PLACE_KM[m["kind"]], SAME_PLACE_KM[r["kind"]])), None)
            if twin is None:
                mine.append(dict(r))
            else:
                twin["pop"] = max(twin["pop"], r["pop"])
                if KIND_ORDER[r["kind"]] < KIND_ORDER[twin["kind"]]:
                    twin["kind"] = r["kind"]
        kept.extend(mine)
    return kept


KIND_ORDER = {"city": 0, "town": 1, "area": 2}


def home_ground_points(rows):
    """Where each Home Ground city is: the biggest place of that name in its country."""
    by_name = defaultdict(list)
    for r in rows:
        if r["kind"] in ("city", "town"):
            by_name[(r["country"], _fold(r["name"]))].append(r)
    pts, missing = {}, []
    seen = set()
    for name, cc in CITY_LIST:
        cid = slug(name)
        if cid in seen:
            cid = f"{cid}_{cc}"
        seen.add(cid)
        cands = by_name.get((cc, _fold(name))) or by_name.get((cc, _fold(OTHER_NAMES.get(name, ""))))
        if not cands:
            missing.append(f"{name} ({cc})")
            continue
        best = max(cands, key=lambda r: (r["kind"] == "city", r["pop"]))
        pts[cid] = (best["lat"], best["lng"], cc, best["id"])
    return pts, missing


def link_cities(rows, pts):
    by_cc = defaultdict(list)
    for cid, (lat, lng, cc, pid) in pts.items():
        by_cc[cc].append((cid, lat, lng, pid))
    for r in rows:
        best = None
        for cid, lat, lng, pid in by_cc.get(r["country"], []):
            if r["id"] == pid:
                best = (0.0, cid)
                break
            if r["kind"] == "area":
                d = km((r["lat"], r["lng"]), (lat, lng))
                if d <= AREA_TO_CITY_KM and (best is None or d < best[0]):
                    best = (d, cid)
        r["city"] = best[1] if best else None


def main(argv):
    geo = argv[0]
    rows = from_geonames(geo)
    nearest = Nearest(geo)
    seen = set()
    for path in argv[1:]:
        for r in from_osm(path, nearest):
            if r["id"] not in seen:          # boxes overlap: a node can come twice
                seen.add(r["id"])
                rows.append(r)
    rows = dedupe(rows)
    pts, missing = home_ground_points(rows)
    link_cities(rows, pts)
    rows.sort(key=lambda r: (r["country"], KIND_ORDER[r["kind"]], -r["pop"], r["name"]))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"source": "GeoNames (CC BY 4.0), © OpenStreetMap contributors (ODbL)",
                             "columns": ["id", "name", "kind", "country", "lat", "lng", "city", "pop"]},
                            ensure_ascii=False) + "\n")
        for r in rows:
            fh.write(json.dumps([r["id"], r["name"], r["kind"], r["country"], r["lat"], r["lng"], r["city"], r["pop"]],
                                ensure_ascii=False, separators=(",", ":")) + "\n")
    counts = defaultdict(int)
    for r in rows:
        counts[r["kind"]] += 1
    print(json.dumps({"places": len(rows), "by_kind": counts, "home_ground_cities_placed": len(pts),
                      "home_ground_cities_missing": missing}, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
