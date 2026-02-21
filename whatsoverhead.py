# whatsoverhead.py

from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Optional, Any, Dict, List, Tuple
import requests
from math import radians, cos, sin, asin, sqrt, atan2, degrees
import os
import json
import hmac
import re
import time
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from pathlib import Path

import logging
from google.cloud.logging_v2.handlers import CloudLoggingHandler
from google.cloud import logging as gcp_logging

try:
    from google.cloud import firestore
except Exception:
    firestore = None

try:
    from google.cloud import secretmanager
except Exception:
    secretmanager = None

#
# env
#

load_dotenv(override=True)
ADSB_API = os.getenv("ADSB_API")
APP_NAME = os.getenv("APP_NAME")
APP_VERSION = os.getenv("APP_VERSION")
DEV = os.getenv("DEV")
DISTANCE = os.getenv("DISTANCE")
GCP_LOG = os.getenv("GCP_LOG")
FIRESTORE_DB = os.getenv("FIRESTORE_DB")
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "airport_cache")
FIRESTORE_LOCK_DOC = os.getenv("FIRESTORE_LOCK_DOC", "locks/poller")
POLL_SLEEP_MS = int(os.getenv("POLL_SLEEP_MS", "1100"))
POLL_LEASE_SECONDS = int(os.getenv("POLL_LEASE_SECONDS", "55"))
ACTIVE_VIEW_WINDOW_SECONDS = int(os.getenv("ACTIVE_VIEW_WINDOW_SECONDS", str(7 * 24 * 60 * 60)))
AIRPORTS_CONFIG_PATH = os.getenv("AIRPORTS_CONFIG_PATH", "config/airports.json")
POLL_SHARED_SECRET = os.getenv("POLL_SHARED_SECRET")
POLL_SECRET_NAME = os.getenv("POLL_SECRET_NAME")
AIRPORT_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
VIEW_STATS_VERSION = 2

#
# app
#

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# cors
allowed_origins = [
    "https://whatsoverhead.rickt.dev",
    "https://whatsoverhead-dev.rickt.dev",
    "https://takingoff.rickt.dev",
    "https://takingoff-dev.rickt.dev",
    "https://lax.takingoff.rickt.dev",
    "https://lax.takingoff-dev.rickt.dev"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# get real IP addr
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts="*"
)

# static dir
app.mount("/static", StaticFiles(directory="static"), name="static")

#
# gcp logging client
#

logging_client = gcp_logging.Client()
logger = logging_client.logger(GCP_LOG)

# capture uvicorn logs
uvicorn_error_name = f"{GCP_LOG}_error"
uvicorn_access_name = f"{GCP_LOG}_access"

uvicorn_error_handler = CloudLoggingHandler(client=logging_client, name=uvicorn_error_name)
uvicorn_access_handler = CloudLoggingHandler(client=logging_client, name=uvicorn_access_name)

uvicorn_error_logger = logging.getLogger("uvicorn.error")
uvicorn_error_logger.setLevel(logging.INFO)
uvicorn_error_logger.addHandler(uvicorn_error_handler)

uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.setLevel(logging.INFO)
uvicorn_access_logger.addHandler(uvicorn_access_handler)

# root logs go to gcp_log
root_handler = CloudLoggingHandler(client=logging_client, name=GCP_LOG)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(root_handler)

# say hello
log_entry = {
    "message": f"{APP_NAME} v{APP_VERSION} starting up",
    "severity": "INFO"
}
logger.log_struct(log_entry)

#
# templates
#

templates = Jinja2Templates(directory="templates")

#
# classes
#

class AircraftRequest(BaseModel):
    lat: float = Field(..., description="latitude of the location.")
    lon: float = Field(..., description="longitude of the location.")
    dist: float = Field(5.0, description="search radius in kilometers (default: 5 km).")
    format: Optional[str] = Field(
        "json",
        description="response format: 'json' or 'text'. defaults to 'json'."
    )

class AircraftResponse(BaseModel):
    flight: str
    desc: str
    alt_baro: Optional[str] = None
    alt_geom: Optional[int] = None
    gs: Optional[int] = None
    track: Optional[int] = None
    year: Optional[int] = None
    ownop: Optional[str] = None
    distance_km: float
    bearing: int
    relative_speed_knots: Optional[float] = None
    message: Optional[str] = None

#
# funcs
#

_firestore_client = None
_secret_manager_client = None
_cached_poll_secret = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_firestore_client():
    global _firestore_client

    if _firestore_client is not None:
        return _firestore_client

    if firestore is None:
        raise HTTPException(status_code=500, detail="Firestore library is not available.")

    project_id = os.getenv("GCP_PROJECT_ID")
    client_kwargs: Dict[str, Any] = {}
    if project_id:
        client_kwargs["project"] = project_id
    if FIRESTORE_DB:
        client_kwargs["database"] = FIRESTORE_DB

    _firestore_client = firestore.Client(**client_kwargs)
    return _firestore_client


def get_secretmanager_client():
    global _secret_manager_client

    if _secret_manager_client is not None:
        return _secret_manager_client

    if secretmanager is None:
        raise HTTPException(status_code=500, detail="Secret Manager library is not available.")

    _secret_manager_client = secretmanager.SecretManagerServiceClient()
    return _secret_manager_client


def resolve_secret_version_name(secret_name: str) -> str:
    raw = (secret_name or "").strip()
    if not raw:
        raise HTTPException(status_code=500, detail="POLL_SECRET_NAME is empty.")

    if raw.startswith("projects/"):
        if "/versions/" in raw:
            return raw
        return f"{raw.rstrip('/')}/versions/latest"

    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        raise HTTPException(
            status_code=500,
            detail="GCP_PROJECT_ID is required when POLL_SECRET_NAME is not a full resource path.",
        )

    return f"projects/{project_id}/secrets/{raw}/versions/latest"


def get_poll_secret_value() -> Optional[str]:
    global _cached_poll_secret

    if _cached_poll_secret is not None:
        return _cached_poll_secret

    if POLL_SECRET_NAME:
        try:
            client = get_secretmanager_client()
            secret_version_name = resolve_secret_version_name(POLL_SECRET_NAME)
            response = client.access_secret_version(request={"name": secret_version_name})
            secret = response.payload.data.decode("utf-8").strip()
        except HTTPException:
            raise
        except Exception as e:
            logger.log_struct(
                {
                    "message": "failed to fetch poll secret from Secret Manager",
                    "secret_name": POLL_SECRET_NAME,
                    "error": get_error_text(e),
                    "severity": "ERROR",
                }
            )
            raise HTTPException(status_code=500, detail="Failed to load poll secret from Secret Manager.")

        if not secret:
            raise HTTPException(status_code=500, detail="Poll secret is empty.")

        _cached_poll_secret = secret
        return _cached_poll_secret

    if POLL_SHARED_SECRET:
        _cached_poll_secret = POLL_SHARED_SECRET
        return _cached_poll_secret

    return None


def get_lock_doc_ref(client):
    parts = [p for p in FIRESTORE_LOCK_DOC.split("/") if p]
    if len(parts) < 2 or len(parts) % 2 != 0:
        raise ValueError(
            "FIRESTORE_LOCK_DOC must be a document path like 'locks/poller' "
            f"(got '{FIRESTORE_LOCK_DOC}')"
        )

    ref = client.collection(parts[0]).document(parts[1])
    for i in range(2, len(parts), 2):
        ref = ref.collection(parts[i]).document(parts[i + 1])
    return ref


def acquire_poller_lock(client, holder: str, run_id: str) -> Tuple[bool, Optional[datetime]]:
    lock_ref = get_lock_doc_ref(client)
    now = utc_now()
    lease_until = now + timedelta(seconds=POLL_LEASE_SECONDS)

    transaction = client.transaction()

    @firestore.transactional
    def txn(txn_obj):
        snap = lock_ref.get(transaction=txn_obj)
        if snap.exists:
            data = snap.to_dict() or {}
            current_lease = data.get("leaseUntil")
            if isinstance(current_lease, datetime):
                if current_lease.tzinfo is None:
                    current_lease = current_lease.replace(tzinfo=timezone.utc)
                else:
                    current_lease = current_lease.astimezone(timezone.utc)
                if current_lease > now:
                    return False, current_lease

        txn_obj.set(
            lock_ref,
            {
                "leaseUntil": lease_until,
                "updatedAt": now,
                "holder": holder,
                "runId": run_id,
            },
            merge=True,
        )
        return True, None

    return txn(transaction)


def release_poller_lock(client, holder: str):
    lock_ref = get_lock_doc_ref(client)
    now = utc_now()
    lock_ref.set(
        {
            "leaseUntil": now,
            "updatedAt": now,
            "holder": holder,
        },
        merge=True,
    )


def load_airports_config() -> Dict[str, Any]:
    config_path = Path(AIRPORTS_CONFIG_PATH)
    if not config_path.is_absolute():
        config_path = Path(__file__).resolve().parent / config_path

    if not config_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Airports config not found at '{config_path}'.",
        )

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid airports config JSON: {e}",
        )

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Airports config must be a JSON object.")
    return data


def _to_float(value: Any, field_name: str, airport_key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field_name} for airport '{airport_key}': {value!r}")


def _normalize_track_degrees(value: Any, field_name: str, airport_key: str) -> float:
    raw = _to_float(value, field_name, airport_key)
    return raw % 360.0


def _angular_diff_degrees(a: float, b: float) -> float:
    diff = abs((a - b) % 360.0)
    return min(diff, 360.0 - diff)


def resolve_airport_fetch_config(airport_key: str, airport_cfg: Dict[str, Any]) -> Tuple[float, float, float]:
    views = airport_cfg.get("views")
    if not isinstance(views, dict) or not views:
        raise ValueError(f"Airport '{airport_key}' has no configured views.")

    first_view_cfg = next(iter(views.values()))
    if not isinstance(first_view_cfg, dict):
        raise ValueError(f"Airport '{airport_key}' has invalid first view config.")

    fetch_cfg = airport_cfg.get("fetch") or {}
    if not isinstance(fetch_cfg, dict):
        raise ValueError(f"Airport '{airport_key}' has invalid fetch config.")

    fetch_lat = fetch_cfg.get("lat", first_view_cfg.get("lat"))
    fetch_lon = fetch_cfg.get("lon", first_view_cfg.get("lon"))
    fetch_dist_km = fetch_cfg.get("dist_km", 15)

    lat = _to_float(fetch_lat, "fetch.lat", airport_key)
    lon = _to_float(fetch_lon, "fetch.lon", airport_key)
    dist_km = _to_float(fetch_dist_km, "fetch.dist_km", airport_key)

    return lat, lon, dist_km


def build_empty_aircraft_payload(message: str) -> Dict[str, Any]:
    return {
        "flight": "N/A",
        "desc": message,
        "alt_baro": None,
        "alt_geom": None,
        "gs": None,
        "track": None,
        "year": None,
        "ownop": None,
        "distance_km": 0.0,
        "bearing": 0,
        "relative_speed_knots": None,
        "message": message,
    }


def build_aircraft_payload(
    nearest_aircraft: Dict[str, Any], center_lat: float, center_lon: float, distance_km: float
) -> Dict[str, Any]:
    flight = nearest_aircraft.get('flight', 'N/A').strip()
    desc = nearest_aircraft.get('desc', 'Unknown TIS-B aircraft')
    alt_baro = nearest_aircraft.get('alt_baro')
    alt_geom = nearest_aircraft.get('alt_geom')
    gs = nearest_aircraft.get('gs')
    track = nearest_aircraft.get('track')
    year = nearest_aircraft.get('year')
    ownop = nearest_aircraft.get('ownOp')
    aircraft_lat = nearest_aircraft.get('lat')
    aircraft_lon = nearest_aircraft.get('lon')
    bearing = calculate_bearing(center_lat, center_lon, aircraft_lat, aircraft_lon)
    distance_km = round(distance_km, 1)

    # ground speed
    if isinstance(gs, (int, float)):
        gs = int(round(gs))
    else:
        gs = None

    # track speed
    if isinstance(track, (int, float)):
        track = int(round(track))
    else:
        track = None

    # determine which altitude to use
    if alt_baro is not None and not isinstance(alt_baro, str):
        used_altitude = alt_baro
    elif alt_geom is not None:
        used_altitude = alt_geom
    else:
        used_altitude = None

    # calculate relative speed based on ground speed and track
    if gs is not None and track is not None:
        relative_speed_knots = calculate_relative_speed(gs, track, bearing)
    else:
        relative_speed_knots = None

    # build the message parts
    message_parts = [f"{flight} is a"]
    if year:
        message_parts.append(f"{year}")
    message_parts.append(f"{desc}")
    if ownop:
        message_parts.append(f"operated by {ownop}")

    direction = get_ordinal_direction(bearing)
    message_parts.append(f"at bearing {bearing}º ({direction}),")

    if used_altitude is not None:
        message_parts.append(f"{distance_km} kilometers away at {used_altitude}ft,")
    else:
        message_parts.append(f"{distance_km} kilometers away,")

    if gs is not None:
        message_parts.append(f"speed {gs} knots,")
    if track is not None:
        message_parts.append(f"ground track {track}º,")

    if relative_speed_knots is not None:
        if relative_speed_knots > 0:
            message_parts.append(f"approaching at {relative_speed_knots:.0f} knots.")
        elif relative_speed_knots < 0:
            message_parts.append(f"receding at {abs(relative_speed_knots):.0f} knots.")
        else:
            message_parts.append("maintaining distance.")

    final_msg = ' '.join(message_parts)

    return {
        "flight": flight,
        "desc": desc,
        "alt_baro": alt_baro,
        "alt_geom": alt_geom,
        "gs": gs,
        "track": track,
        "year": year,
        "ownop": ownop,
        "distance_km": distance_km,
        "bearing": bearing,
        "relative_speed_knots": relative_speed_knots,
        "message": final_msg,
    }


def get_error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def validate_poll_secret(request: Request):
    # If configured, require the poll secret from Secret Manager (or fallback env var).
    expected_secret = get_poll_secret_value()
    if not expected_secret:
        return

    presented = request.headers.get("X-Poll-Secret")
    if not presented or not hmac.compare_digest(presented, expected_secret):
        raise HTTPException(status_code=401, detail="Unauthorized poll request.")


def build_cached_results_for_airport(
    airport_key: str, airport_cfg: Dict[str, Any], aircraft_list: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    views = airport_cfg.get("views")
    if not isinstance(views, dict) or not views:
        raise ValueError(f"Airport '{airport_key}' has no configured views.")

    results: List[Dict[str, Any]] = []
    for view_name, view_cfg in views.items():
        if not isinstance(view_cfg, dict):
            raise ValueError(f"Airport '{airport_key}' view '{view_name}' is invalid.")

        lat = _to_float(view_cfg.get("lat"), f"views.{view_name}.lat", airport_key)
        lon = _to_float(view_cfg.get("lon"), f"views.{view_name}.lon", airport_key)
        dist_km = _to_float(view_cfg.get("dist", 3.5), f"views.{view_name}.dist", airport_key)
        map_zoom = _to_float(view_cfg.get("mapZoom", 12.5), f"views.{view_name}.mapZoom", airport_key)

        max_alt_value = view_cfg.get("max_alt", 8500)
        movement_value = view_cfg.get("movement", "receding")
        expected_track_value = view_cfg.get("expected_track")
        track_tolerance_value = view_cfg.get("track_tolerance", 35)

        max_alt: Optional[float] = None
        if max_alt_value is not None:
            max_alt = _to_float(max_alt_value, f"views.{view_name}.max_alt", airport_key)

        expected_track: Optional[float] = None
        if expected_track_value is not None:
            expected_track = _normalize_track_degrees(
                expected_track_value, f"views.{view_name}.expected_track", airport_key
            )

        track_tolerance: Optional[float] = None
        if expected_track is not None:
            track_tolerance = _to_float(
                track_tolerance_value, f"views.{view_name}.track_tolerance", airport_key
            )

        movement = None if movement_value is None else str(movement_value)

        nearest_aircraft, distance_km = find_nearest_aircraft(
            aircraft_list,
            lat,
            lon,
            max_alt=max_alt,
            movement=movement,
            max_distance_km=dist_km,
            expected_track=expected_track,
            track_tolerance=track_tolerance,
        )

        is_primary = bool(view_cfg.get("primary", view_name == "departures"))
        strict_match = nearest_aircraft is not None
        is_active = strict_match

        if strict_match:
            payload = build_aircraft_payload(nearest_aircraft, lat, lon, distance_km)
            message = payload["message"]
        elif is_primary:
            # Fallback: if no runway-aligned match, still show closest plausible departure nearby.
            # This avoids blank panels while keeping strict matching as the primary signal.
            fallback_nearest, fallback_distance_km = find_nearest_aircraft(
                aircraft_list,
                lat,
                lon,
                max_alt=max_alt,
                movement=movement,
                max_distance_km=max(dist_km, 7.5),
                expected_track=None,
                track_tolerance=None,
            )
            if fallback_nearest is not None:
                fallback_payload = build_aircraft_payload(fallback_nearest, lat, lon, fallback_distance_km)
                message = (
                    "No runway-aligned departure found.\n\n"
                    f"Closest departing aircraft: {fallback_payload['message']}"
                )
            else:
                message = "No aircraft found within the specified radius."
        else:
            message = "No runway-aligned departure found."

        results.append(
            {
                "view": view_name,
                "label": view_cfg.get("label", view_name),
                "message": message,
                "lat": lat,
                "lon": lon,
                "mapZoom": map_zoom,
                "primary": is_primary,
                "active": is_active,
            }
        )

    return results


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def filter_cached_results_for_recency(cached_doc: Dict[str, Any]) -> Dict[str, Any]:
    doc = dict(cached_doc or {})
    raw_results = doc.get("results")
    if not isinstance(raw_results, list):
        doc["results"] = []
        return doc

    meta = doc.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        doc["meta"] = meta

    stats_version = _coerce_int(meta.get("view_stats_version"))
    view_stats_raw = meta.get("view_stats") if stats_version == VIEW_STATS_VERSION else {}
    view_stats = view_stats_raw if isinstance(view_stats_raw, dict) else {}

    now_epoch = int(utc_now().timestamp())
    cutoff_epoch = now_epoch - max(ACTIVE_VIEW_WINDOW_SECONDS, 0)
    filtered: List[Dict[str, Any]] = []

    for result in raw_results:
        if not isinstance(result, dict):
            continue

        view_name = str(result.get("view", "")).strip()
        if not view_name:
            continue

        stats = view_stats.get(view_name)
        if not isinstance(stats, dict):
            stats = {}

        last_positive_at = _coerce_int(stats.get("lastPositiveAt"))
        primary_from_stats = bool(stats.get("primary"))
        primary = bool(result.get("primary")) or primary_from_stats or view_name == "departures"
        active = bool(result.get("active"))
        seen_recently = last_positive_at is not None and last_positive_at >= cutoff_epoch

        if primary or active or seen_recently:
            filtered.append(
                {
                    "view": view_name,
                    "label": result.get("label", view_name),
                    "message": result.get("message", ""),
                    "lat": result.get("lat"),
                    "lon": result.get("lon"),
                    "mapZoom": result.get("mapZoom"),
                }
            )

    if not filtered:
        # Backward-compatible fallback so callers always have something to render.
        first = next((r for r in raw_results if isinstance(r, dict)), None)
        if first:
            filtered.append(
                {
                    "view": first.get("view", "departures"),
                    "label": first.get("label", "Departures"),
                    "message": first.get("message", "No aircraft found within the specified radius."),
                    "lat": first.get("lat"),
                    "lon": first.get("lon"),
                    "mapZoom": first.get("mapZoom"),
                }
            )

    doc["results"] = filtered
    meta["active_view_window_seconds"] = max(ACTIVE_VIEW_WINDOW_SECONDS, 0)
    meta["view_stats_version"] = VIEW_STATS_VERSION
    return doc

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    # calculate the bearing between two lat/lon points
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    delta_lon = radians(lon2 - lon1)

    x = sin(delta_lon) * cos(lat2_rad)
    y = cos(lat1_rad) * sin(lat2_rad) - sin(lat1_rad) * cos(lat2_rad) * cos(delta_lon)

    initial_bearing_rad = atan2(x, y)
    initial_bearing_deg = (degrees(initial_bearing_rad) + 360) % 360
    return int(round(initial_bearing_deg))

def calculate_relative_speed(gs: float, aircraft_track: float, user_to_aircraft_bearing: float) -> float:
    # calculate the bearing from aircraft to user
    aircraft_to_user_bearing = (user_to_aircraft_bearing + 180) % 360
    # calculate the angle difference between the aircraft's track and the bearing to user
    angle_diff = aircraft_track - aircraft_to_user_bearing
    # normalize the angle to be within [-180, 180]
    angle_diff = (angle_diff + 180) % 360 - 180
    # calculate the relative speed
    relative_speed = gs * cos(radians(angle_diff))
    return relative_speed  # positive: approaching, negative: moving away

def find_nearest_aircraft(
    aircraft_list: list,
    center_lat: float,
    center_lon: float,
    max_alt: float = None,
    movement: str = None,
    max_distance_km: float = None,
    expected_track: float = None,
    track_tolerance: float = None,
):
    # find the nearest aircraft that is airborne with valid speed
    if not aircraft_list:
        return None, None

    nearest = None
    min_distance = float('inf')

    for aircraft in aircraft_list:
        # get the geometric & barometric altitude and ground speed
        alt_geom = aircraft.get('alt_geom')
        alt_baro = aircraft.get('alt_baro')
        gs = aircraft.get('gs')
        track = aircraft.get('track')

        # exclude aircraft on the ground
        if isinstance(alt_baro, str) and alt_baro.lower() == "ground":
            continue

        # determine aircraft's altitude by various means
        if alt_baro is not None:
            try:
                altitude = float(alt_baro)
            except (ValueError, TypeError):
                continue
        elif alt_geom is not None:
            try:
                altitude = float(alt_geom)
            except (ValueError, TypeError):
                continue
        else:
            continue

        # skip if altitude < 100ft
        if altitude <= 100:
            continue

        # skip if altitude > max_alt (if provided)
        if max_alt is not None and altitude > max_alt:
            continue

        if gs is None or gs == 0:
            continue

        # skip if no lat/lon
        aircraft_lat = aircraft.get('lat')
        aircraft_lon = aircraft.get('lon')
        if aircraft_lat is None or aircraft_lon is None:
            continue

        if expected_track is not None:
            if track is None:
                continue
            tol = 35.0 if track_tolerance is None else float(track_tolerance)
            if _angular_diff_degrees(float(track), float(expected_track)) > tol:
                continue

        # filter by movement (receding/approaching)
        if movement:
            if track is None:
                continue
            
            # calculate bearing and relative speed for filtering
            bearing = calculate_bearing(center_lat, center_lon, aircraft_lat, aircraft_lon)
            rel_speed = calculate_relative_speed(gs, track, bearing)
            
            if movement.lower() == "receding" and rel_speed >= 0:
                continue # skip if approaching or stationary
            elif movement.lower() == "approaching" and rel_speed <= 0:
                continue # skip if receding or stationary

        # calculate the distance
        distance_km = haversine_distance(center_lat, center_lon, aircraft_lat, aircraft_lon)
        if max_distance_km is not None and distance_km > max_distance_km:
            continue
        if distance_km < min_distance:
            min_distance = distance_km
            nearest = aircraft

    if nearest:
        log_entry = {
            "flight": nearest.get('flight', 'N/A').strip(),
            "description": nearest.get('desc', 'Unknown TIS-B aircraft'),
            "altitude_baro": nearest.get('alt_baro'),
            "altitude_geom": nearest.get('alt_geom'),
            "ground_speed": nearest.get('gs'),
            "track": nearest.get('track'),
            "year": nearest.get('year'),
            "operator": nearest.get('ownOp'),
            "distance_km": min_distance,
            "bearing_deg": calculate_bearing(center_lat, center_lon, nearest.get('lat'), nearest.get('lon')),
            "timestamp": datetime.utcnow().isoformat() + 'Z'
        }
        logger.log_struct(log_entry, severity="INFO")

    return nearest, min_distance

def get_aircraft_data(lat: float, lon: float, dist: float):
    # set the external ads-b api url
    url = f"{ADSB_API}/lat/{lat}/lon/{lon}/dist/{dist}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Error fetching data from ads-b api: {e}")
    except ValueError:
        raise HTTPException(status_code=502, detail="Error decoding json response from ads-b api.")

def get_ordinal_direction(bearing: int) -> str:
    # determine the ordinal direction from the bearing
    direction_index = int(((bearing + 22.5) % 360) // 45)
    directions = ["north", "northeast", "east", "southeaast", "south", "southwest", "west", "northweest"]
    return directions[direction_index]

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # calculates the distance between two points using the haversine formula
    r_km = 6371.0
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(radians, [lat1, lon1, lat2, lon2])
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = sin(dlat/2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    distance_km = r_km * c
    return distance_km

#
# api endpoints
#

# home page render
@app.get("/")
@app.get("/index.html")
def render_whatsoverhead(request: Request):
    # return different HTML if dev or prod (!dev)
    if DEV == "True":
        return templates.TemplateResponse("whatsoverhead_dev.html", {"request": request})
    else:
        return templates.TemplateResponse("whatsoverhead.html", {"request": request})

# healthcheck
@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/healthz")
def healthz_check():
    return {"status": "ok"}


@app.get("/airports")
def list_airports():
    airports = load_airports_config()
    items: List[Dict[str, Any]] = []

    for key_raw, cfg in airports.items():
        key = str(key_raw).strip().lower()
        if not key:
            continue
        if not isinstance(cfg, dict):
            continue

        code = str(cfg.get("code", key)).upper()
        title = str(cfg.get("title", f"What's Taking Off at {code}?"))
        views = cfg.get("views")
        view_count = len(views) if isinstance(views, dict) else 0

        items.append(
            {
                "key": key,
                "code": code,
                "title": title,
                "view_count": view_count,
            }
        )

    items.sort(key=lambda x: x["code"])
    return {"airports": items, "count": len(items)}


@app.get("/cached")
def get_cached_airport(code: str, response: Response):
    airport_key = (code or "").strip().lower()
    if not airport_key or not AIRPORT_KEY_RE.match(airport_key):
        raise HTTPException(status_code=400, detail="Missing or invalid code.")

    try:
        client = get_firestore_client()
        snap = client.collection(FIRESTORE_COLLECTION).document(airport_key).get()
    except HTTPException:
        raise
    except Exception as e:
        logger.log_struct(
            {
                "message": "cached read failed",
                "airport_key": airport_key,
                "error": get_error_text(e),
                "severity": "ERROR",
            }
        )
        raise HTTPException(status_code=500, detail="Failed to read cache.")

    if not snap.exists:
        raise HTTPException(status_code=404, detail=f"No cache available for code '{airport_key}'.")

    response.headers["Cache-Control"] = "public, max-age=5"
    cached_doc = snap.to_dict() or {}
    return filter_cached_results_for_recency(cached_doc)


@app.post("/poll")
def poll_cache(request: Request):
    validate_poll_secret(request)

    run_started = utc_now()
    run_id = run_started.isoformat().replace("+00:00", "Z")
    holder = request.headers.get("X-CloudScheduler-JobName") or request.headers.get("User-Agent") or "poller"

    updated: List[str] = []
    skipped: List[str] = []
    errors: List[Dict[str, str]] = []

    try:
        client = get_firestore_client()
    except HTTPException:
        raise
    except Exception as e:
        logger.log_struct(
            {
                "message": "poll failed to create firestore client",
                "run_id": run_id,
                "error": get_error_text(e),
                "severity": "ERROR",
            }
        )
        raise HTTPException(status_code=500, detail="Failed to initialize Firestore client.")

    try:
        acquired, lease_until = acquire_poller_lock(client, holder=holder, run_id=run_id)
    except Exception as e:
        logger.log_struct(
            {
                "message": "poll failed to acquire lock",
                "run_id": run_id,
                "error": get_error_text(e),
                "severity": "ERROR",
            }
        )
        raise HTTPException(status_code=500, detail="Failed to acquire poll lock.")

    if not acquired:
        logger.log_struct(
            {
                "message": "poll skipped due to active lease",
                "run_id": run_id,
                "lease_until": lease_until.isoformat().replace("+00:00", "Z") if lease_until else None,
                "severity": "INFO",
            }
        )
        return {
            "ok": True,
            "skipped_all": True,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "run_id": run_id,
        }

    provider_calls = 0

    try:
        airports = load_airports_config()
        airport_items = list(airports.items())

        for idx, (airport_key_raw, airport_cfg) in enumerate(airport_items):
            airport_key = str(airport_key_raw).strip().lower()

            if not airport_key or not AIRPORT_KEY_RE.match(airport_key):
                err = "Invalid airport key."
                errors.append({"code": str(airport_key_raw), "error": err})
                skipped.append(str(airport_key_raw))
                continue

            if not isinstance(airport_cfg, dict):
                err = "Invalid airport config."
                errors.append({"code": airport_key, "error": err})
                skipped.append(airport_key)
                continue

            did_provider_call = False
            try:
                fetch_lat, fetch_lon, fetch_dist_km = resolve_airport_fetch_config(airport_key, airport_cfg)

                existing_doc: Dict[str, Any] = {}
                try:
                    existing_snap = client.collection(FIRESTORE_COLLECTION).document(airport_key).get()
                    if existing_snap.exists:
                        existing_doc = existing_snap.to_dict() or {}
                except Exception as e:
                    logger.log_struct(
                        {
                            "message": "failed to read existing cache doc before update",
                            "run_id": run_id,
                            "airport_key": airport_key,
                            "error": get_error_text(e),
                            "severity": "WARNING",
                        }
                    )

                provider_t0 = time.time()
                did_provider_call = True
                provider_calls += 1
                provider_data = get_aircraft_data(fetch_lat, fetch_lon, fetch_dist_km)
                provider_ms = int((time.time() - provider_t0) * 1000)

                aircraft_list = provider_data.get("aircraft", []) if isinstance(provider_data, dict) else []
                if not isinstance(aircraft_list, list):
                    aircraft_list = []

                results = build_cached_results_for_airport(airport_key, airport_cfg, aircraft_list)
                now_epoch = int(utc_now().timestamp())
                prev_meta = existing_doc.get("meta") if isinstance(existing_doc.get("meta"), dict) else {}
                prev_stats_version = _coerce_int(prev_meta.get("view_stats_version"))
                prev_stats_raw = (
                    prev_meta.get("view_stats")
                    if prev_stats_version == VIEW_STATS_VERSION and isinstance(prev_meta.get("view_stats"), dict)
                    else {}
                )
                view_stats: Dict[str, Dict[str, Any]] = {}

                for result in results:
                    view_name = str(result.get("view", "")).strip()
                    if not view_name:
                        continue

                    prev_stat = prev_stats_raw.get(view_name)
                    if not isinstance(prev_stat, dict):
                        prev_stat = {}

                    last_positive_at = _coerce_int(prev_stat.get("lastPositiveAt"))
                    if bool(result.get("active")):
                        last_positive_at = now_epoch

                    view_stats[view_name] = {
                        "lastPositiveAt": last_positive_at,
                        "primary": bool(result.get("primary", False)),
                    }

                cache_doc = {
                    "airportKey": airport_key,
                    "code": airport_cfg.get("code", airport_key.upper()),
                    "title": airport_cfg.get("title", f"What's Taking Off at {airport_key.upper()}?"),
                    "updatedAt": now_epoch,
                    "provider": "adsb.fi",
                    "results": results,
                    "meta": {
                        "fetch_lat": fetch_lat,
                        "fetch_lon": fetch_lon,
                        "fetch_dist_km": fetch_dist_km,
                        "aircraft_count": len(aircraft_list),
                        "poll_run_id": run_id,
                        "provider_call_ms": provider_ms,
                        "view_stats": view_stats,
                        "view_stats_version": VIEW_STATS_VERSION,
                    },
                }

                client.collection(FIRESTORE_COLLECTION).document(airport_key).set(cache_doc)
                updated.append(airport_key)

                logger.log_struct(
                    {
                        "message": "airport cache updated",
                        "run_id": run_id,
                        "airport_key": airport_key,
                        "provider_call_ms": provider_ms,
                        "aircraft_count": len(aircraft_list),
                        "result_count": len(results),
                        "severity": "INFO",
                    }
                )
            except Exception as e:
                err_text = get_error_text(e)
                errors.append({"code": airport_key, "error": err_text})
                logger.log_struct(
                    {
                        "message": "airport poll failed",
                        "run_id": run_id,
                        "airport_key": airport_key,
                        "error": err_text,
                        "severity": "ERROR",
                    }
                )
            finally:
                # Enforce headroom against the provider's 1 req/sec limit.
                if did_provider_call and idx < (len(airport_items) - 1):
                    time.sleep(max(POLL_SLEEP_MS, 0) / 1000.0)
    except Exception as e:
        err_text = get_error_text(e)
        errors.append({"code": "_poll", "error": err_text})
        logger.log_struct(
            {
                "message": "poll failed before completion",
                "run_id": run_id,
                "error": err_text,
                "severity": "ERROR",
            }
        )
    finally:
        try:
            release_poller_lock(client, holder=holder)
        except Exception as e:
            logger.log_struct(
                {
                    "message": "failed to release poll lock (lease will expire)",
                    "run_id": run_id,
                    "error": get_error_text(e),
                    "severity": "ERROR",
                }
            )

    logger.log_struct(
        {
            "message": "poll completed",
            "run_id": run_id,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "provider_calls": provider_calls,
            "severity": "INFO",
        }
    )

    return {
        "ok": True,
        "skipped_all": False,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "run_id": run_id,
    }


# find nearest plane
@app.get("/nearest_plane")
def nearest_plane(
    request: Request,  # added to capture incoming request
    lat: float,
    lon: float,
    dist: Optional[float] = 5.0,
    max_alt: Optional[float] = None,
    movement: Optional[str] = None,
    format: Optional[str] = "text"
):

    if dist is None:
        dist = float(DISTANCE)

    data = get_aircraft_data(lat, lon, dist)
    aircraft_list = data.get('aircraft', [])

    # no aircraft returned, sorry
    if not aircraft_list:
        payload = build_empty_aircraft_payload("No aircraft found within the specified radius.")
        if format.lower() == "text":
            return Response(content=payload["message"], media_type="text/plain")
        return AircraftResponse(**payload)

    nearest_aircraft, distance_km = find_nearest_aircraft(aircraft_list, lat, lon, max_alt, movement)
    if not nearest_aircraft:
        payload = build_empty_aircraft_payload("No aircraft found within the specified radius.")
        if format.lower() == "text":
            return Response(content=payload["message"], media_type="text/plain")
        return AircraftResponse(**payload)

    payload = build_aircraft_payload(nearest_aircraft, lat, lon, distance_km)

    # log it
    logger.log_struct(
        {
            "message": payload["message"],
            "severity": "INFO"
        }
    )

    # text requested, return simple
    if format.lower() == "text":
        return Response(content=payload["message"] + "\n", media_type="text/plain")

    # return fancy
    return AircraftResponse(**payload)

# EOF
