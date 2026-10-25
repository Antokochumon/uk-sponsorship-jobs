"""Local website/API for the JobSpy UK job collector."""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from werkzeug.security import check_password_hash

from apps_script_import import AppsScriptClient, AppsScriptImportError
from collect_jobs import CollectorConfig, collect_jobs
from job_search import _employer_key, search_live_jobs, sponsorship_evidence

load_dotenv()

ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = ROOT.parent / "frontend"
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "").strip()
# Keep local compatibility while deployments migrate to the single variable.
APP_SCRIPT_READ_URL = APPS_SCRIPT_URL or os.getenv("APPS_SCRIPT_READ_URL", "").strip()
APP_SCRIPT_IMPORT_URL = APPS_SCRIPT_URL or os.getenv("APPS_SCRIPT_IMPORT_URL", "").strip() or APP_SCRIPT_READ_URL
JOBSPY_IMPORT_TOKEN = os.getenv("JOBSPY_IMPORT_TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
ALLOWED_ORIGINS = {item.strip().rstrip("/") for item in os.getenv("ALLOWED_ORIGINS", "").split(",") if item.strip()}
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1").strip().lower() in {"1", "true", "yes"}
try:
    IMPORT_BATCH_SIZE = min(100, max(1, int(os.getenv("IMPORT_BATCH_SIZE", "50"))))
except ValueError:
    IMPORT_BATCH_SIZE = 50

collection_lock = threading.Lock()
import_test_verified = False
admin_sessions: dict[str, float] = {}
login_failures: dict[str, list[float]] = {}
collection_state: dict[str, Any] = {"running": False, "phase": "idle", "logs": [], "result": None, "startedAt": None, "finishedAt": None}
schedule_state: dict[str, Any] = {"hours": None, "nextRun": None}

# Process-local cache for public Google Sheets feeds. Render Free supports this
# in memory; entries are intentionally disposable and are rebuilt after restart.
try:
    SHEET_CACHE_SECONDS = min(3600, max(30, int(os.getenv("SHEET_CACHE_SECONDS", "300"))))
except ValueError:
    SHEET_CACHE_SECONDS = 300
_sheet_cache_lock = threading.Lock()
_sheet_cache: dict[str, dict[str, Any]] = {}
_sheet_cache_refreshing: set[str] = set()
_sheet_cache_events: dict[str, threading.Event] = {}
_sheet_cache_errors: dict[str, tuple[float, str, int]] = {}
_sheet_cache_retry_after: dict[str, float] = {}


def _fetch_sheet_payload(cache_key: str, action: str | None, required_array: str) -> None:
    """Fetch and validate one public sheet feed, retaining the last good copy."""
    params: dict[str, Any] = {"_": int(time.time() * 1000)}
    if action:
        params["action"] = action
    try:
        if not APP_SCRIPT_READ_URL:
            raise RuntimeError("The Google Sheets feed is not configured.")
        response = requests.get(
            APP_SCRIPT_READ_URL,
            params=params,
            headers={"Accept": "application/json"},
            timeout=(10, 45),
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get(required_array), list):
            raise ValueError(f"Apps Script feed does not contain a {required_array} array.")
        with _sheet_cache_lock:
            _sheet_cache[cache_key] = {"payload": payload, "cached_at": time.monotonic()}
            _sheet_cache_errors.pop(cache_key, None)
            _sheet_cache_retry_after.pop(cache_key, None)
    except requests.Timeout:
        with _sheet_cache_lock:
            failed_at = time.monotonic()
            _sheet_cache_errors[cache_key] = (failed_at, "The Google Sheets feed timed out.", 502)
            _sheet_cache_retry_after[cache_key] = failed_at + 60
    except requests.RequestException:
        with _sheet_cache_lock:
            failed_at = time.monotonic()
            _sheet_cache_errors[cache_key] = (failed_at, "The Google Sheets feed is temporarily unavailable.", 502)
            _sheet_cache_retry_after[cache_key] = failed_at + 60
    except ValueError:
        with _sheet_cache_lock:
            failed_at = time.monotonic()
            _sheet_cache_errors[cache_key] = (failed_at, "Apps Script feed did not return valid data.", 502)
            _sheet_cache_retry_after[cache_key] = failed_at + 60
    except RuntimeError as exc:
        with _sheet_cache_lock:
            failed_at = time.monotonic()
            _sheet_cache_errors[cache_key] = (failed_at, str(exc), 503)
            _sheet_cache_retry_after[cache_key] = failed_at + 60
    finally:
        with _sheet_cache_lock:
            _sheet_cache_refreshing.discard(cache_key)
            event = _sheet_cache_events.get(cache_key)
            if event:
                event.set()


def _get_sheet_payload(cache_key: str, action: str | None, required_array: str) -> tuple[dict[str, Any] | None, str | None, int]:
    """Return a cached feed; coalesce cold requests and refresh stale data safely."""
    now = time.monotonic()
    with _sheet_cache_lock:
        entry = _sheet_cache.get(cache_key)
        if entry and now - entry["cached_at"] < SHEET_CACHE_SECONDS:
            return entry["payload"], None, 200
        if entry and now < _sheet_cache_retry_after.get(cache_key, 0):
            return entry["payload"], None, 200
        if cache_key in _sheet_cache_refreshing:
            event = _sheet_cache_events[cache_key]
            owner = False
        else:
            event = threading.Event()
            _sheet_cache_events[cache_key] = event
            _sheet_cache_refreshing.add(cache_key)
            owner = True
        has_stale = entry is not None

    if has_stale:
        # Serve the last good data immediately while refreshing in the background.
        if owner:
            threading.Thread(
                target=_fetch_sheet_payload,
                args=(cache_key, action, required_array),
                name=f"sheet-cache-{cache_key}-refresh",
                daemon=True,
            ).start()
        return entry["payload"], None, 200

    if owner:
        _fetch_sheet_payload(cache_key, action, required_array)
    else:
        # On a cold start, wait for the single in-flight fetch rather than
        # making every simultaneous visitor hit Apps Script independently.
        event.wait(timeout=50)

    with _sheet_cache_lock:
        entry = _sheet_cache.get(cache_key)
        if entry:
            return entry["payload"], None, 200
        error = _sheet_cache_errors.get(cache_key)
        if error:
            return None, error[1], error[2]
    return None, "The Google Sheets feed is temporarily unavailable.", 502


def _invalidate_sheet_cache(*keys: str) -> None:
    """Invalidate after an admin write so the next public request fetches updates."""
    with _sheet_cache_lock:
        selected = keys or tuple(_sheet_cache.keys())
        for key in selected:
            _sheet_cache.pop(key, None)
            _sheet_cache_errors.pop(key, None)
            _sheet_cache_retry_after.pop(key, None)


def _sheet_cache_refresh_loop() -> None:
    """Refresh previously loaded feeds while the Render web process is awake."""
    feed_specs = {"jobs": (None, "jobs"), "sponsors": ("sponsors", "companies")}
    while True:
        time.sleep(15)
        if not APP_SCRIPT_READ_URL:
            continue
        now = time.monotonic()
        with _sheet_cache_lock:
            due_keys = [
                key for key, entry in _sheet_cache.items()
                if key in feed_specs
                and now - entry["cached_at"] >= SHEET_CACHE_SECONDS
                and now >= _sheet_cache_retry_after.get(key, 0)
                and key not in _sheet_cache_refreshing
            ]
        for key in due_keys:
            action, required_array = feed_specs[key]
            # This returns the last good payload immediately and starts one
            # background refresh, so visitors don't wait for a scheduled fetch.
            _get_sheet_payload(key, action, required_array)


app = Flask(__name__, static_folder=None)

CORS(
    app,
    resources={r"/api/*": {"origins": list(ALLOWED_ORIGINS)}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Accept", "X-CSRF-Token"],
    methods=["GET", "POST", "OPTIONS"],
)

app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="jobapi_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="None" if SESSION_COOKIE_SECURE else "Lax",
)


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; connect-src 'self' https://api.postcodes.io; "
        "img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept, X-CSRF-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response


def _admin_authenticated() -> bool:
    return bool(session.get("admin_authenticated"))


def _csrf_valid() -> bool:
    return bool(session.get("csrf_token")) and secrets.compare_digest(
        str(session.get("csrf_token")), request.headers.get("X-CSRF-Token", "")
    )


def _admin_required():
    if not SECRET_KEY or not (ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
        return jsonify({"error": "Admin authentication is not configured on the backend."}), 503
    if not _admin_authenticated():
        return jsonify({"error": "Admin authentication required."}), 401
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_valid():
        return jsonify({"error": "A valid CSRF token is required."}), 403
    return None


COLLECTION_CATEGORIES = {
    "all": "All jobs",
    "licensed_sponsors_offering_sponsorship": "Licensed sponsors offering sponsorship",
    "no_sponsorship_mentioned": "Jobs with no sponsorship mentioned",
}


def _filter_collection_category(jobs: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    """Filter collected vacancies using the selected admin category."""
    if category == "all":
        return jobs
    if category == "no_sponsorship_mentioned":
        return [
            job for job in jobs
            if sponsorship_evidence(str(job.get("description", "")))[1] == "Sponsorship not stated"
        ]
    if category == "licensed_sponsors_offering_sponsorship":
        payload, error, status = _get_sheet_payload("sponsors", "sponsors", "companies")
        if error or not payload:
            raise RuntimeError(error or f"Unable to load the licensed sponsor register (HTTP {status}).")
        sponsor_names = {
            _employer_key(str(row.get("employerName", "")))
            for row in payload.get("companies", [])
            if isinstance(row, dict) and row.get("employerName")
        }
        sponsor_names.discard("")
        if not sponsor_names:
            raise RuntimeError("The licensed sponsor register returned no company names; no jobs were imported.")
        matches = []
        for job in jobs:
            employer = _employer_key(str(job.get("employer", "")))
            evidence_status = sponsorship_evidence(str(job.get("description", "")))[1]
            if employer in sponsor_names and evidence_status == "Sponsorship confirmed":
                matches.append(job)
        return matches
    raise ValueError("Choose a valid job category.")


def _run_collection(
    max_jobs: int | None = None,
    lock_acquired: bool = False,
    days: int = 28,
    category: str = "all",
) -> None:
    global collection_state
    if not lock_acquired:
        collection_lock.acquire()
    try:
        collection_state.update({"running": True, "phase": "collecting", "logs": ["Collection started."], "result": None, "startedAt": datetime.now(timezone.utc).isoformat(), "finishedAt": None})
        try:
            result = collect_jobs(
                config=CollectorConfig.from_env_with_max_age(days),
                fetch_description=category != "all",
            )
            categorized_jobs = _filter_collection_category(result.jobs, category)
            jobs = categorized_jobs[:max_jobs] if max_jobs else categorized_jobs
            category_label = COLLECTION_CATEGORIES.get(category, COLLECTION_CATEGORIES["all"])
            collection_state["logs"].append(
                f"Category: {category_label}. Collected {len(jobs)} matching jobs from the last {days} day(s); "
                f"{result.duplicate_count} duplicates skipped."
            )
            if not jobs:
                collection_state["phase"] = "complete"
                collection_state["result"] = {**result.as_dict(), "import": {"confirmed": False, "counts": {"submitted": 0, "imported": 0}, "errors": [{"error": f"No recent listings matched the selected category: {category_label}."}]}}
                return
            collection_state["phase"] = "importing"
            client = _apps_script_client()
            if not client.configured:
                raise AppsScriptImportError("Apps Script import is not configured.")
            imported = client.import_jobs(jobs, batch_size=IMPORT_BATCH_SIZE)
            _invalidate_sheet_cache("jobs", "sponsors")
            collection_state["logs"].append(f"Import completed: {imported['counts'].get('imported', 0)} imported.")
            collection_state["result"] = {**result.as_dict(), "import": imported}
            collection_state["phase"] = "complete" if imported.get("confirmed") else "failed"
        except Exception as exc:
            app.logger.exception("Collection failed")
            collection_state["phase"] = "failed"
            collection_state["logs"].append(f"Collection failed: {type(exc).__name__}.")
            collection_state["result"] = {"import": {"confirmed": False, "errors": [{"error": str(exc)}]}}
        finally:
            collection_state["running"] = False
            collection_state["finishedAt"] = datetime.now(timezone.utc).isoformat()
            collection_lock.release()
    except Exception:
        collection_state["running"] = False
        collection_state["phase"] = "failed"
        collection_state["logs"].append("Collection failed before execution started.")
        collection_lock.release()


def _apps_script_client() -> AppsScriptClient:
    return AppsScriptClient(APP_SCRIPT_IMPORT_URL, APP_SCRIPT_READ_URL, JOBSPY_IMPORT_TOKEN)


def _request_is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    return not origin or urlsplit(origin).netloc.lower() == request.host.lower()


@app.get("/")
def index():
    return send_from_directory(FRONTEND_ROOT, "index.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(FRONTEND_ROOT, "admin.html")


@app.get("/licensed-sponsors.html")
def licensed_sponsors_page():
    return send_from_directory(FRONTEND_ROOT, "licensed-sponsors.html")


@app.route("/api/<path:unused>", methods=["OPTIONS"])
def api_options(unused):
    return ("", 204)


@app.post("/api/admin/login")
def admin_login():
    if not SECRET_KEY or not (ADMIN_PASSWORD_HASH or ADMIN_PASSWORD):
        return jsonify({"error": "Admin authentication is not configured on the backend."}), 503
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    now = time.time()
    client_key = request.remote_addr or "unknown"
    attempts = [stamp for stamp in login_failures.get(client_key, []) if now - stamp < 900]
    if len(attempts) >= 5:
        return jsonify({"error": "Too many sign-in attempts. Try again later."}), 429
    try:
        password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password) if ADMIN_PASSWORD_HASH else secrets.compare_digest(password, ADMIN_PASSWORD)
    except (ValueError, TypeError):
        return jsonify({"error": "Admin authentication is not configured on the backend."}), 503
    if not secrets.compare_digest(username, ADMIN_USERNAME) or not password_ok:
        attempts.append(now)
        login_failures[client_key] = attempts
        return jsonify({"error": "Invalid admin credentials."}), 401
    login_failures.pop(client_key, None)
    session.clear()
    session["admin_authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify({"authenticated": True, "csrfToken": session["csrf_token"]})


@app.post("/api/admin/logout")
def admin_logout():
    denied = _admin_required()
    if denied:
        return denied
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/admin/status")
def admin_status():
    denied = _admin_required()
    if denied:
        return denied
    return jsonify({"authenticated": True, "csrfToken": session.get("csrf_token", ""), "collection": collection_state, "schedule": schedule_state})


@app.get("/api/admin/stats")
def admin_stats():
    denied = _admin_required()
    if denied:
        return denied
    try:
        return jsonify(_apps_script_client().admin_action("getStats"))
    except AppsScriptImportError as exc:
        # Older deployed doPost handlers only understand importJobs. Keep the
        # dashboard useful by calculating read-only statistics from doGet.
        try:
            response = requests.get(APP_SCRIPT_READ_URL, params={"_": int(time.time() * 1000)}, timeout=(10, 45))
            response.raise_for_status()
            feed = response.json()
            jobs = feed.get("jobs") if isinstance(feed, dict) else None
            if not isinstance(jobs, list):
                raise ValueError("Apps Script feed did not contain jobs.")
            stats = {"storedJobs": len(jobs), "recentJobs": len(jobs), "licensedSponsorJobs": 0, "advertEvidenceJobs": 0, "verifiedJobs": 0, "unavailableJobs": 0, "licensedSponsors": int((feed.get("sponsorRegister") or {}).get("rowCount") or 0)}
            for job in jobs:
                match = str(job.get("sponsorRegisterMatch", "")).lower().startswith("matched:")
                status = str(job.get("sponsorshipStatus", "")).lower()
                evidence = str(job.get("sponsorshipEvidence", "")).lower()
                unavailable = "unavailable" in status or "no sponsorship offered" in status or "unavailable" in evidence
                positive = "confirmed" in status or "positive sponsorship" in evidence or "sponsorship available" in evidence
                stats["licensedSponsorJobs"] += int(match)
                stats["advertEvidenceJobs"] += int(positive and not unavailable)
                stats["verifiedJobs"] += int(match and positive and not unavailable)
                stats["unavailableJobs"] += int(unavailable)
            return jsonify({"ok": True, "stats": stats, "fallback": True, "maintenanceWarning": str(exc)})
        except (requests.RequestException, ValueError, TypeError, AttributeError) as fallback_error:
            return jsonify({"error": "Dashboard statistics unavailable. Deploy the updated JobSpyImport.gs. " + str(fallback_error)}), 502


@app.post("/api/admin/refresh-sponsors")
def admin_refresh_sponsors():
    denied = _admin_required()
    if denied:
        return denied
    if not collection_lock.acquire(blocking=False):
        return jsonify({"error": "Another sheet operation is already in progress."}), 409
    try:
        result = _apps_script_client().admin_action("refreshSponsors")
        _invalidate_sheet_cache("jobs", "sponsors")
        return jsonify(result)
    except AppsScriptImportError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        collection_lock.release()


@app.post("/api/admin/delete")
def admin_delete():
    denied = _admin_required()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode", "")).strip().lower()
    if mode not in {"one", "all"}:
        return jsonify({"error": "Delete mode must be one or all."}), 400
    if mode == "all" and payload.get("confirm") is not True:
        return jsonify({"error": "Delete-all requires explicit confirmation."}), 400
    job_id = str(payload.get("id", "")).strip()
    if mode == "one" and not job_id:
        return jsonify({"error": "A job ID is required."}), 400
    if not collection_lock.acquire(blocking=False):
        return jsonify({"error": "Another sheet operation is already in progress."}), 409
    try:
        result = _apps_script_client().admin_action("deleteJobs", mode=mode, id=job_id, confirm=payload.get("confirm") is True)
        _invalidate_sheet_cache("jobs", "sponsors")
        return jsonify(result)
    except AppsScriptImportError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        collection_lock.release()


@app.post("/api/admin/collect")
def admin_collect():
    denied = _admin_required()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    try:
        max_jobs = max(1, min(500, int(payload.get("maxJobs", 50))))
    except (TypeError, ValueError):
        return jsonify({"error": "Maximum jobs must be a whole number from 1 to 500."}), 400
    try:
        days = int(payload.get("days", 28))
    except (TypeError, ValueError):
        return jsonify({"error": "Date range must be 1, 3, 7, 14, or 28 days."}), 400
    if days not in {1, 3, 7, 14, 28}:
        return jsonify({"error": "Date range must be 1, 3, 7, 14, or 28 days."}), 400
    category = str(payload.get("category", "all")).strip()
    if category not in COLLECTION_CATEGORIES:
        return jsonify({"error": "Choose a valid job category."}), 400
    if not _apps_script_client().configured:
        return jsonify({"error": "Configure the Apps Script import URL and backend token first."}), 503
    if collection_state["running"] or not collection_lock.acquire(blocking=False):
        return jsonify({"error": "A collection run is already in progress."}), 409
    threading.Thread(target=_run_collection, args=(max_jobs, True, days, category), daemon=True, name="job-collection").start()
    return jsonify({"started": True, "days": days, "category": category}), 202


@app.post("/api/admin/schedule")
def admin_schedule():
    denied = _admin_required()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    value = payload.get("hours")
    if value in (None, "", 0, "0"):
        schedule_state.update({"hours": None, "nextRun": None})
        return jsonify(schedule_state)
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return jsonify({"error": "Schedule must be every 1, 3, 6, 12 or 24 hours."}), 400
    if hours not in {1, 3, 6, 12, 24}:
        return jsonify({"error": "Schedule must be every 1, 3, 6, 12 or 24 hours."}), 400
    schedule_state.update({"hours": hours, "nextRun": time.time() + hours * 3600})
    return jsonify(schedule_state)


@app.get("/api/status")
def status():
    client = _apps_script_client()
    configured = client.configured
    return jsonify({
        "feedConfigured": bool(APPS_SCRIPT_URL or APP_SCRIPT_READ_URL),
        "jobSpyAvailable": True,
        "importAvailable": configured,
        "importTestVerified": import_test_verified,
        "importBlocker": "Configure the import URL/token, deploy JobSpyImport.gs, and pass the one-real-job import/read-back test." if not configured or not import_test_verified else "",
    })


@app.get("/api/jobs")
def jobs():
    if not APP_SCRIPT_READ_URL:
        return jsonify({"error": "The jobs feed is not configured."}), 503
    payload, error, status_code = _get_sheet_payload("jobs", None, "jobs")
    if payload is None:
        return jsonify({"error": error or "The jobs feed is temporarily unavailable."}), status_code
    return jsonify(payload)


@app.get("/api/sponsors")
def sponsors():
    if not APP_SCRIPT_READ_URL:
        return jsonify({"error": "The sponsor directory is not configured."}), 503
    payload, error, status_code = _get_sheet_payload("sponsors", "sponsors", "companies")
    if payload is None:
        return jsonify({"error": error or "The sponsor directory is temporarily unavailable."}), status_code
    return jsonify(payload)


@app.get("/api/search")
def search():
    query = request.args.get("query", "").strip()[:120]
    location = request.args.get("location", "").strip()[:120]
    if not query and not location:
        return jsonify({"error": "Enter a job title or a UK location to search."}), 400
    try:
        radius = int(request.args.get("radius", "10"))
        days = int(request.args.get("days", "28"))
        salary_min = int(request.args["salaryMin"]) if request.args.get("salaryMin") else None
        salary_max = int(request.args["salaryMax"]) if request.args.get("salaryMax") else None
    except ValueError:
        return jsonify({"error": "Radius, date range and salary filters must be whole numbers."}), 400
    if radius not in {5, 10, 25, 50, 100} or days not in {1, 3, 7, 14, 28}:
        return jsonify({"error": "Choose a supported radius and posting-age range."}), 400
    if any(value is not None and not 0 <= value <= 1_000_000 for value in (salary_min, salary_max)):
        return jsonify({"error": "Salary filters must be between £0 and £1,000,000."}), 400
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        return jsonify({"error": "Minimum salary cannot exceed maximum salary."}), 400
    job_type = request.args.get("jobType", "").strip().lower()
    if job_type not in {"", "all", "fulltime", "parttime", "contract", "temporary", "internship"}:
        return jsonify({"error": "Choose a supported job type."}), 400
    latitude = request.args.get("latitude")
    longitude = request.args.get("longitude")
    if bool(latitude) != bool(longitude):
        return jsonify({"error": "Both coordinates are required for Near me search."}), 400
    if latitude and longitude:
        try:
            lat_value, lon_value = float(latitude), float(longitude)
            if not -90 <= lat_value <= 90 or not -180 <= lon_value <= 180:
                raise ValueError
            nearest = requests.get(
                "https://api.postcodes.io/postcodes",
                params={"lat": lat_value, "lon": lon_value, "limit": 1},
                headers={"Accept": "application/json"},
                timeout=(3, 8),
            )
            nearest.raise_for_status()
            matches = nearest.json().get("result") or []
            if not matches or not matches[0].get("postcode"):
                return jsonify({"error": "Could not resolve a nearby UK postcode. Enter a postcode or town instead."}), 422
            location = matches[0]["postcode"]
        except (ValueError, requests.RequestException):
            return jsonify({"error": "Near me lookup failed. Enter a postcode or town instead."}), 422
        except (AttributeError, TypeError):
            return jsonify({"error": "Near me lookup returned an invalid response."}), 502
    if not collection_lock.acquire(blocking=False):
        return jsonify({"error": "A live search is already running. Please wait for it to finish."}), 429
    try:
        payload = search_live_jobs(query, location, radius, days, salary_min, salary_max, "" if job_type in {"", "all"} else job_type)
        if not payload["jobs"] and payload["errors"]:
            return jsonify({**payload, "error": "Job sources are temporarily unavailable."}), 502
        return jsonify(payload)
    except Exception as exc:
        app.logger.warning("Live job search failed (%s)", type(exc).__name__)
        return jsonify({"error": "The live search could not be completed. Try again shortly.", "jobs": []}), 502
    finally:
        collection_lock.release()


@app.post("/api/import-test")
def import_test():
    denied = _admin_required()
    if denied:
        return denied
    global import_test_verified
    client = _apps_script_client()
    if not client.configured:
        return jsonify({"confirmed": False, "error": "Configure APPS_SCRIPT_URL and JOBSPY_IMPORT_TOKEN first."}), 503
    if not collection_lock.acquire(blocking=False):
        return jsonify({"confirmed": False, "error": "A JobSpy collection/import is already running."}), 409
    try:
        result = collect_jobs(smoke=True)
        if not result.jobs:
            import_test_verified = False
            return jsonify({
                "confirmed": False,
                "error": "The real smoke search returned no valid dated UK listing; no test payload was sent.",
                "collection": result.as_dict()["counts"],
            }), 422
        test_job = result.jobs[0]
        verification = client.test_one_real_job(test_job)
        _invalidate_sheet_cache("jobs", "sponsors")
        import_test_verified = bool(verification["confirmed"])
        return jsonify({
            "confirmed": import_test_verified,
            "testJob": {"title": test_job["title"], "employer": test_job["employer"], "source": test_job["source"], "date": test_job["date"]},
            "collection": result.as_dict()["counts"],
            "import": verification,
        }), 200 if import_test_verified else 502
    except AppsScriptImportError as exc:
        import_test_verified = False
        return jsonify({"confirmed": False, "error": str(exc)}), 502
    except Exception as exc:
        import_test_verified = False
        return jsonify({"confirmed": False, "error": f"Connection test failed ({type(exc).__name__})."}), 502
    finally:
        collection_lock.release()


@app.post("/api/collect")
def collect():
    denied = _admin_required()
    if denied:
        return denied
    if not import_test_verified:
        return jsonify({"error": "Run and pass the one-real-job Sheets connection test before importing collected listings."}), 409
    client = _apps_script_client()
    if not client.configured:
        return jsonify({"error": "Configure the Apps Script import URL and local token first."}), 503
    if not collection_lock.acquire(blocking=False):
        return jsonify({"error": "A JobSpy collection/import is already running."}), 409

    try:
        try:
            result = collect_jobs()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:
            return jsonify({"error": f"JobSpy collection failed ({type(exc).__name__})."}), 502

        if not result.jobs:
            return jsonify({
                **result.as_dict(),
                "import": {
                    "confirmed": False,
                    "counts": {"submitted": 0, "received": 0, "inserted": 0, "updated": 0, "duplicates": 0, "rejected": 0, "imported": 0},
                    "verifiedSaved": 0,
                    "errors": [{"error": "No valid recent listings were collected; nothing was sent to Apps Script."}],
                },
            })
        try:
            import_result = client.import_jobs(result.jobs, batch_size=IMPORT_BATCH_SIZE)
            _invalidate_sheet_cache("jobs", "sponsors")
        except AppsScriptImportError as exc:
            import_result = {
                "confirmed": False,
                "counts": {"submitted": len(result.jobs), "received": 0, "inserted": 0, "updated": 0, "duplicates": 0, "rejected": 0, "imported": 0},
                "verifiedSaved": 0,
                "errors": [{"error": str(exc)}],
            }
        return jsonify({**result.as_dict(), "import": import_result}), 200 if import_result["confirmed"] else 502
    finally:
        collection_lock.release()


def _scheduler_loop() -> None:
    while True:
        time.sleep(15)
        hours = schedule_state.get("hours")
        next_run = schedule_state.get("nextRun")
        if not hours or not next_run or time.time() < next_run:
            continue
        if not collection_state["running"] and _apps_script_client().configured and collection_lock.acquire(blocking=False):
            threading.Thread(target=_run_collection, args=(None, True), daemon=True, name="scheduled-collection").start()
        schedule_state["nextRun"] = time.time() + hours * 3600


# Gunicorn (Render) imports this module rather than running it as __main__, so
# start the lightweight cache refresher at import time as well as local runs.
threading.Thread(target=_sheet_cache_refresh_loop, daemon=True, name="sheet-cache-scheduler").start()

if __name__ == "__main__":
    threading.Thread(target=_scheduler_loop, daemon=True, name="collection-scheduler").start()
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "5001"))
    app.run(host=host, port=port, debug=False)
    
