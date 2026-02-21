import csv
import json
import math
from pathlib import Path

EARTH_R = 6371000.0  # meters

def move_point(lat_deg, lon_deg, bearing_deg, distance_m):
    """Move lat/lon along initial bearing by distance (meters). Returns (lat, lon)."""
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    brng = math.radians(bearing_deg)
    dr = distance_m / EARTH_R

    lat2 = math.asin(math.sin(lat1) * math.cos(dr) + math.cos(lat1) * math.sin(dr) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(dr) * math.cos(lat1),
                             math.cos(dr) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)

def load_runways(runways_csv_path):
    runways = []
    with open(runways_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            runways.append(r)
    return runways

def find_runway_end(runways, airport_ident, depart_runway_ident):
    """
    Given e.g. airport_ident='KLAX', depart_runway_ident='25L'
    choose the matching record and return the threshold lat/lon and heading for that end.
    OurAirports has le_* and he_* ends. If depart ident matches le_ident use le coords and le_heading_degT.
    If it matches he_ident use he coords and he_heading_degT.
    """
    for r in runways:
        if r.get("airport_ident") != airport_ident:
            continue

        le_ident = r.get("le_ident")
        he_ident = r.get("he_ident")

        if depart_runway_ident == le_ident:
            lat = float(r["le_latitude_deg"])
            lon = float(r["le_longitude_deg"])
            hdg = float(r["le_heading_degT"])
            return lat, lon, hdg

        if depart_runway_ident == he_ident:
            lat = float(r["he_latitude_deg"])
            lon = float(r["he_longitude_deg"])
            hdg = float(r["he_heading_degT"])
            return lat, lon, hdg

    raise ValueError(f"Runway end not found: airport_ident={airport_ident} depart_runway_ident={depart_runway_ident}")

def main():
    # Edit this mapping to match your “dominant takeoff runway(s)”.
    # For paired parallels, include both ends and we'll average the two 500m-ahead points.
    dominant = {
        "lax": {"airport_ident": "KLAX", "depart_ends": ["25L", "25R"], "label": "Departures (West Flow • RWY 25L/25R)", "title": "What's Taking Off at LAX?"},
        "jfk": {"airport_ident": "KJFK", "depart_ends": ["31L", "31R"], "label": "Departures (NW Flow • RWY 31L/31R)", "title": "What's Taking Off at JFK?"},
        "lga": {"airport_ident": "KLGA", "depart_ends": ["31"], "label": "Departures (NW Flow • RWY 31)", "title": "What's Taking Off at LaGuardia (LGA)?"},
        "lhr": {"airport_ident": "EGLL", "depart_ends": ["27L", "27R"], "label": "Departures (West Flow • RWY 27L/27R)", "title": "What's Taking Off at Heathrow (LHR)?"},
        "lgw": {"airport_ident": "EGKK", "depart_ends": ["26L", "26R"], "label": "Departures (West Flow • RWY 26)", "title": "What's Taking Off at Gatwick (LGW)?"},
        "cdg": {"airport_ident": "LFPG", "depart_ends": ["26L", "26R", "27L", "27R"], "label": "Departures (West Flow • RWY 26/27 Complex)", "title": "What's Taking Off at Paris CDG (CDG)?"},
        "hnd": {"airport_ident": "RJTT", "depart_ends": ["23"], "label": "Departures (Primary • RWY 23)", "title": "What's Taking Off at Haneda (HND)?"},
        "nrt": {"airport_ident": "RJAA", "depart_ends": ["34L", "34R"], "label": "Departures (Primary • RWY 34L/34R)", "title": "What's Taking Off at Narita (NRT)?"},
    }

    script_dir = Path(__file__).resolve().parent
    runways = load_runways(script_dir / "runways.csv")

    out = {}
    for key, cfg in dominant.items():
        pts = []
        for end_ident in cfg["depart_ends"]:
            lat, lon, hdg = find_runway_end(runways, cfg["airport_ident"], end_ident)
            lat2, lon2 = move_point(lat, lon, hdg, 500.0)  # 500m after runway end
            pts.append((lat2, lon2))

        # average points if multiple ends provided (paired parallels / complex)
        avg_lat = sum(p[0] for p in pts) / len(pts)
        avg_lon = sum(p[1] for p in pts) / len(pts)

        out[key] = {
            "code": key.upper(),
            "title": cfg["title"],
            "views": {
                "departures": {
                    "label": cfg["label"],
                    "lat": round(avg_lat, 6),
                    "lon": round(avg_lon, 6),
                    "dist": 3.5,
                    "mapZoom": 12.5,
                }
            },
        }

    out_path = script_dir.parent / "config" / "airports.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
