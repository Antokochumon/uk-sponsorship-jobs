"""Collect recent UK vacancies from job boards supported by JobSpy."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


DEFAULT_SEARCH_TERMS = [
    "care assistant OR healthcare assistant OR care worker OR support worker OR nurse OR senior care assistant OR home care worker OR residential support worker",
    "warehouse operative OR cleaner OR customer service OR kitchen assistant OR hospitality assistant OR production operative",
    "software developer OR data analyst OR administrator OR engineer OR accountant",
    "teacher OR teaching assistant",
    '"visa sponsorship" jobs',
]
DEFAULT_LOCATIONS = ["England", "Scotland", "Wales", "Northern Ireland"]
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)


@dataclass
class CollectorConfig:
    search_terms: list[str] = field(default_factory=lambda: DEFAULT_SEARCH_TERMS.copy())
    locations: list[str] = field(default_factory=lambda: DEFAULT_LOCATIONS.copy())
    sites: list[str] = field(default_factory=lambda: ["indeed"])
    results_per_search: int = 15
    max_searches_per_run: int = 20
    max_age_days: int = 28
    request_delay_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "CollectorConfig":
        return cls(
            search_terms=_csv_env("SEARCH_TERMS", DEFAULT_SEARCH_TERMS),
            locations=_csv_env("SEARCH_LOCATIONS", DEFAULT_LOCATIONS),
            sites=_csv_env("JOBSPY_SITES", ["indeed"]),
            results_per_search=_int_env("RESULTS_PER_SEARCH", 15, minimum=1),
            max_searches_per_run=_int_env("MAX_SEARCHES_PER_RUN", 20, minimum=1),
            max_age_days=_int_env("MAX_AGE_DAYS", 28, minimum=1),
            request_delay_seconds=_float_env("REQUEST_DELAY_SECONDS", 2.0, minimum=0),
        )


@dataclass
class CollectionResult:
    jobs: list[dict[str, Any]]
    searches: int
    duplicate_count: int
    rejected_missing_date: int
    rejected_invalid_date: int
    rejected_out_of_window: int
    rejected_invalid_url: int
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobs": self.jobs,
            "counts": {
                "collected": len(self.jobs),
                "searches": self.searches,
                "duplicates": self.duplicate_count,
                "missingDate": self.rejected_missing_date,
                "invalidDate": self.rejected_invalid_date,
                "outsideDateWindow": self.rejected_out_of_window,
                "invalidUrl": self.rejected_invalid_url,
                "errors": len(self.errors),
            },
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _csv_env(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "").strip()
    return [part.strip() for part in value.split(",") if part.strip()] if value else default.copy()


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _posted_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime_time.min)
    else:
        text = _text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_url(value: Any) -> str:
    raw = _text(value)
    try:
        parts = urlsplit(raw)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        host = parts.hostname.lower()
        if host.startswith("www."):
            host = host[4:]
        port = parts.port
        netloc = host if not port or (parts.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
        ]
        return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def _sponsorship_fields(description: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", description).strip()
    lower = text.lower()
    negative_terms = (
        "does not offer visa sponsorship",
        "cannot offer visa sponsorship",
        "unable to provide sponsorship",
        "no visa sponsorship",
        "not able to sponsor",
        "sponsorship is not available",
    )
    for phrase in negative_terms:
        if phrase in lower:
            return "Advert states sponsorship is unavailable", "Sponsorship unavailable"

    positive_terms = (
        "visa sponsorship",
        "sponsorship available",
        "sponsorship offered",
        "sponsor visa",
        "certificate of sponsorship",
        "skilled worker sponsorship",
    )
    for phrase in positive_terms:
        position = lower.find(phrase)
        if position >= 0:
            excerpt = text[max(0, position - 70):min(len(text), position + len(phrase) + 110)]
            return f"Positive sponsorship wording found in advert: {excerpt}", "Unverified licence"
    return "No explicit sponsorship statement found in the collected advert.", "Sponsorship not stated"


def _workplace_type(row: dict[str, Any]) -> str:
    value = _text(row.get("work_from_home_type"))
    if value:
        return value.title()
    remote = row.get("is_remote")
    if remote is True or _text(remote).lower() in {"true", "1", "yes"}:
        return "Remote"
    return "Not specified"


def normalize_job(row: dict[str, Any], now: datetime, max_age_days: int) -> tuple[dict[str, Any] | None, str]:
    raw_date = row.get("date_posted")
    if not _text(raw_date):
        return None, "missing_date"
    posted = _posted_datetime(raw_date)
    if posted is None:
        return None, "invalid_date"
    age = now - posted
    if age < timedelta(0) or age > timedelta(days=max_age_days):
        return None, "outside_date_window"

    source_url = canonical_url(row.get("job_url"))
    application_url = canonical_url(row.get("job_url_direct")) or source_url
    if not application_url:
        return None, "invalid_url"

    site = _text(row.get("site")) or "Other"
    raw_id = _text(row.get("id"))
    stable_id = f"{site.lower()}:{raw_id}" if raw_id else hashlib.sha256(application_url.encode("utf-8")).hexdigest()
    description = _text(row.get("description"))
    evidence, sponsorship_status = _sponsorship_fields(description)
    location = _text(row.get("location"))
    postcode_match = UK_POSTCODE.search(location)
    timestamp = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    return {
        "id": stable_id,
        "employer": _text(row.get("company")) or "Employer not supplied",
        "title": _text(row.get("title")) or "Title not supplied",
        "location": location or "UK location not supplied",
        "date": posted.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dateType": "Job board posting date",
        "sponsorshipEvidence": evidence,
        "sponsorshipStatus": sponsorship_status,
        "sponsorRegisterMatch": "Not checked",
        "applicationUrl": application_url,
        "sourceUrl": source_url or application_url,
        "url": application_url,
        "source": site.replace("_", " ").title(),
        "description": description,
        "workplaceType": _workplace_type(row),
        "postcode": postcode_match.group(0).upper() if postcode_match else "",
        "lastChecked": timestamp,
        "firstDiscovered": timestamp,
    }, ""


def _row_dicts(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "to_dict"):
        return result.to_dict(orient="records")
    return [dict(row) for row in result]


def collect_jobs(config: CollectorConfig | None = None, smoke: bool = False) -> CollectionResult:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        raise RuntimeError("JobSpy is not installed. Install dependencies from requirements.txt.") from exc

    config = config or CollectorConfig.from_env()
    terms = config.search_terms[:1] if smoke else config.search_terms
    locations = config.locations[:1] if smoke else config.locations
    result_limit = min(config.results_per_search, 5) if smoke else config.results_per_search
    pairs = [(term, location) for location in locations for term in terms]
    pairs = pairs[:1] if smoke else pairs[:config.max_searches_per_run]
    now = datetime.now(timezone.utc)
    jobs_by_url: dict[str, dict[str, Any]] = {}
    jobs_by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    rejected: dict[str, int] = {
        "missing_date": 0,
        "invalid_date": 0,
        "outside_date_window": 0,
        "invalid_url": 0,
    }
    duplicate_count = 0

    for search_index, (term, location) in enumerate(pairs):
        try:
            frame = scrape_jobs(
                site_name=config.sites,
                search_term=term,
                location=location,
                results_wanted=result_limit,
                hours_old=config.max_age_days * 24,
                country_indeed="UK",
                fetch_description=False,
                verbose=0,
            )
        except Exception as exc:
            errors.append({"location": location, "searchTerm": term, "error": type(exc).__name__})
            continue

        for row in _row_dicts(frame):
            normalized, reason = normalize_job(row, now, config.max_age_days)
            if normalized is None:
                rejected[reason] += 1
                continue
            key_url = canonical_url(normalized["sourceUrl"])
            key_id = normalized["id"]
            existing = jobs_by_url.get(key_url) or jobs_by_id.get(key_id)
            if existing is not None:
                duplicate_count += 1
                if len(normalized["description"]) > len(existing["description"]):
                    existing["description"] = normalized["description"]
                    existing["sponsorshipEvidence"], existing["sponsorshipStatus"] = _sponsorship_fields(normalized["description"])
                continue
            jobs_by_url[key_url] = normalized
            jobs_by_id[key_id] = normalized

        for site, message in getattr(frame, "attrs", {}).get("errors", {}).items():
            errors.append({"location": location, "searchTerm": term, "source": str(site), "error": str(message)[:180]})
        for site, messages in getattr(frame, "attrs", {}).get("warnings", {}).items():
            warnings.append({"location": location, "source": str(site), "warning": str(messages)[:180]})
        if config.request_delay_seconds and search_index < len(pairs) - 1:
            time.sleep(config.request_delay_seconds)

    return CollectionResult(
        jobs=list(jobs_by_url.values()),
        searches=len(pairs),
        duplicate_count=duplicate_count,
        rejected_missing_date=rejected["missing_date"],
        rejected_invalid_date=rejected["invalid_date"],
        rejected_out_of_window=rejected["outside_date_window"],
        rejected_invalid_url=rejected["invalid_url"],
        errors=errors,
        warnings=warnings,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect real, recent UK job listings with JobSpy.")
    parser.add_argument("--smoke", action="store_true", help="Run one small real-data search for validation.")
    parser.add_argument("--summary-only", action="store_true", help="Do not print listing titles after the summary.")
    args = parser.parse_args()

    try:
        result = collect_jobs(smoke=args.smoke)
    except RuntimeError as exc:
        parser.error(str(exc))
    print(json.dumps(result.as_dict()["counts"], indent=2))
    if result.errors:
        print("Search errors:")
        print(json.dumps(result.errors, indent=2))
    if not args.summary_only:
        for job in result.jobs[:5]:
            print(f"{job['source']} | {job['date']} | {job['title']} | {job['employer']}")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
