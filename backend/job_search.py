"""Interactive UK job search adapters and official sponsor-register matching."""

from __future__ import annotations

import csv
import difflib
import io
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from collect_jobs import canonical_url, normalize_job

JOBS_SEARCH_URL = "https://www.jobs.service.gov.uk/jobs/search"
JOBS_BASE_URL = "https://www.jobs.service.gov.uk"
SPONSOR_REGISTER_PAGE = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
REQUEST_HEADERS = {"User-Agent": "UKJobFinder/1.0", "Accept": "text/html,application/xhtml+xml,application/json"}
RESULT_LIMIT = 15
SEARCH_TERMS = (
    "care assistant", "care worker", "support worker", "healthcare assistant", "nurse",
    "warehouse operative", "cleaner", "customer service", "kitchen assistant", "software developer",
    "data analyst", "administrator", "engineer", "accountant", "teacher", "teaching assistant",
)
ROLE_EXPANSIONS = {
    "care": ("care assistant", "care worker", "support worker"),
    "carer": ("care assistant", "care worker", "support worker"),
    "nurse": ("registered nurse", "staff nurse", "nursing assistant"),
    "warehouse": ("warehouse operative", "warehouse assistant", "picker packer"),
    "cleaner": ("cleaning operative", "domestic assistant"),
    "developer": ("software developer", "software engineer"),
    "teacher": ("teacher", "teaching assistant"),
}
SPONSOR_CACHE_SECONDS = 24 * 60 * 60
_sponsor_cache: tuple[float, set[str], str, int] | None = None
_sponsor_lock = threading.Lock()


def correct_query(query: str) -> str:
    words = query.split()
    vocabulary = {word for phrase in SEARCH_TERMS for word in phrase.split()}
    corrected: list[str] = []
    for word in words:
        clean = re.sub(r"[^a-zA-Z'-]", "", word)
        if len(clean) >= 4:
            match = difflib.get_close_matches(clean.lower(), vocabulary, n=1, cutoff=0.82)
            if match:
                word = re.sub(re.escape(clean), match[0], word, count=1, flags=re.IGNORECASE)
        corrected.append(word)
    return " ".join(corrected)


def search_terms(query: str) -> list[str]:
    corrected = correct_query(query.strip())
    terms = [query.strip()]
    if corrected.lower() != query.strip().lower():
        terms.insert(0, corrected)
    for token in re.findall(r"[a-z]+", corrected.lower()):
        terms.extend(ROLE_EXPANSIONS.get(token, ()))
    return list(dict.fromkeys(term for term in terms if term))[:4]


def relevance_score(job: dict[str, Any], query: str) -> int:
    if not query.strip():
        return 0
    title = str(job.get("title", "")).lower()
    description = str(job.get("description", "")).lower()
    requested = query.strip().lower()
    corrected = correct_query(query).lower()
    if title == requested:
        return 120
    if requested in title:
        return 105
    if corrected != requested and corrected in title:
        return 100
    expansions = [term.lower() for term in search_terms(query) if term.lower() not in {requested, corrected}]
    if any(term in title for term in expansions):
        return 70
    if any(token in title for token in re.findall(r"[a-z]+", corrected)):
        return 45
    if requested in description or corrected in description:
        return 10
    return 0


def _safe_url(value: str, base: str = "") -> str:
    candidate = urljoin(base, value.strip()) if base else value.strip()
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname
            and "." in parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            return candidate
    except ValueError:
        pass
    return ""


def _parse_posted_date(value: str, now: datetime) -> str:
    text = re.sub(r"^Added on\s+", "", value.strip(), flags=re.IGNORECASE)
    for pattern in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            if now - timedelta(days=28) <= parsed <= now + timedelta(days=1):
                return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
            return ""
        except ValueError:
            continue
    return ""


def parse_gov_jobs(html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.select('a[href*="/jobs/"]'):
        href = link.get("href", "")
        source_url = _safe_url(href, JOBS_BASE_URL)
        try:
            source_parts = urlsplit(source_url)
        except ValueError:
            continue
        if (
            source_parts.hostname not in {"jobs.service.gov.uk", "www.jobs.service.gov.uk"}
            or not re.fullmatch(r"/jobs/[a-fA-F0-9]{20,40}", source_parts.path)
        ):
            continue
        if not source_url or source_url in seen:
            continue
        title = link.get_text(" ", strip=True)
        if not title or title.lower() in {"read more", "apply"}:
            continue
        card = link.find_parent("li") or link.parent
        for _ in range(3):
            if card and len(card.get_text(" ", strip=True)) > len(title) + 20:
                break
            if card and card.parent:
                card = card.parent
        lines = [re.sub(r"\s+", " ", line).strip() for line in card.get_text("\n", strip=True).splitlines() if line.strip()] if card else [title]
        title_index = next((index for index, line in enumerate(lines) if line == title), 0)
        employer_location = lines[title_index + 1] if title_index + 1 < len(lines) else ""
        if " - " in employer_location:
            employer, location = employer_location.rsplit(" - ", 1)
        else:
            employer, location = employer_location or "Employer not supplied", "UK"
        added = next((line for line in lines if re.match(r"Added on\s+", line, re.IGNORECASE)), "")
        posted = _parse_posted_date(added, now)
        if not posted:
            continue
        type_text = " ".join(lines[title_index + 2:title_index + 6]).lower()
        type_match = re.search(r"full[ -]?time|part[ -]?time|contract|temporary|internship", type_text)
        job_type = type_match.group(0).replace("-", " ").replace("fulltime", "full time").replace("parttime", "part time").title() if type_match else ""
        results.append({
            "id": source_url.rsplit("/", 1)[-1], "title": title, "employer": employer,
            "location": location, "date": posted,
            "salary": next((line for line in lines if "£" in line), ""), "jobType": job_type,
            "description": " ".join(lines[title_index + 2:])[:1200], "source": "UK Government Jobs",
            "sourceUrl": source_url, "applicationUrl": source_url, "url": source_url,
        })
        seen.add(source_url)
    return results


def sponsorship_evidence(description: str) -> tuple[str, str]:
    """Classify the complete vacancy text, with negative wording first."""
    text = re.sub(r"\s+", " ", description).strip()
    negative = re.search(
        r"(?:no|not\s+(?:currently\s+)?(?:available|offered|provided)|cannot|can't|unable\s+to|do\s+not|does\s+not|will\s+not|won't|not\s+able\s+to)"
        r"(?:\s+currently)?(?:\s+offer|\s+provide|\s+support|\s+give|\s+sponsor)?\s+(?:a\s+)?(?:visa\s+|work\s+|skilled\s+worker\s+)?"
        r"(?:sponsorship|sponsor(?:ship)?|certificate\s+of\s+sponsorship|CoS)\b|"
        r"(?:sponsorship|visa\s+sponsorship)\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+offered|not\s+provided)",
        text,
        re.IGNORECASE,
    )
    if negative:
        excerpt = text[max(0, negative.start() - 65):min(len(text), negative.end() + 90)]
        return f"Advert states sponsorship is unavailable: {excerpt}", "Sponsorship unavailable"
    positive = re.search(
        r"(?:visa|skilled\s+worker|work\s+visa|certificate\s+of\s+sponsorship|CoS)\s+"
        r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided|considered|supported)|"
        r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided|considered|supported)|"
        r"(?:offer|provide|support|consider|sponsor)\w*(?:\s+visa)?\s+(?:visa\s+)?sponsorship|"
        r"(?:we|employer)\s+(?:can|will)\s+sponsor",
        text,
        re.IGNORECASE,
    )
    if positive:
        excerpt = text[max(0, positive.start() - 65):min(len(text), positive.end() + 90)]
        return f"Explicit sponsorship wording found in advert: {excerpt}", "Sponsorship confirmed"
    return "No explicit sponsorship offer found in the complete advert text.", "Sponsorship not stated"


def _employer_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    return re.sub(r"\b(limited|ltd|plc|llp|incorporated|inc)\b$", "", normalized).strip()


def _load_sponsor_register() -> tuple[set[str], str, int]:
    global _sponsor_cache
    now = time.monotonic()
    with _sponsor_lock:
        if _sponsor_cache and now - _sponsor_cache[0] < SPONSOR_CACHE_SECONDS:
            return _sponsor_cache[1], _sponsor_cache[2], _sponsor_cache[3]
        try:
            page = requests.get(SPONSOR_REGISTER_PAGE, headers=REQUEST_HEADERS, timeout=(5, 15))
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "html.parser")
            csv_url = next(
                (_safe_url(anchor.get("href", ""), SPONSOR_REGISTER_PAGE)
                 for anchor in soup.select("a[href]")
                 if anchor.get("href", "").lower().endswith(".csv") and "worker" in anchor.get_text(" ", strip=True).lower()),
                "",
            )
            if not csv_url:
                raise ValueError("Official register CSV link was not found")
            response = requests.get(csv_url, headers={**REQUEST_HEADERS, "Accept": "text/csv"}, timeout=(5, 30))
            response.raise_for_status()
            text = response.content.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            columns = reader.fieldnames or []
            name_column = next((column for column in columns if "organisation name" in column.lower() or "organization name" in column.lower()), None)
            if not name_column:
                raise ValueError("Official register has no recognizable organisation-name column")
            names = {_employer_key(row.get(name_column, "")) for row in reader if _employer_key(row.get(name_column, ""))}
            _sponsor_cache = (time.monotonic(), names, csv_url, len(names))
            return names, csv_url, len(names)
        except (requests.RequestException, ValueError, csv.Error):
            return set(), "", 0


def annotate_sponsorship(job: dict[str, Any], sponsor_names: set[str]) -> dict[str, Any]:
    evidence, status = sponsorship_evidence(str(job.get("description", "")))
    employer = str(job.get("employer", ""))
    matched = _employer_key(employer) in sponsor_names if sponsor_names else False
    if status == "Sponsorship unavailable":
        sponsor_status = status
    elif status == "Sponsorship confirmed" and matched:
        sponsor_status = status
    elif matched:
        sponsor_status = "Sponsorship not stated"
    else:
        sponsor_status = "Unverified licence"
    job["sponsorshipEvidence"] = evidence
    job["sponsorshipStatus"] = sponsor_status
    job["sponsorRegisterMatch"] = f"Matched: {employer}" if matched else ("No exact employer match" if sponsor_names else "Not checked; register unavailable")
    return job


def _salary_value(raw: dict[str, Any]) -> tuple[float | None, float | None, str]:
    minimum = raw.get("min_amount") or raw.get("salary_min")
    maximum = raw.get("max_amount") or raw.get("salary_max")
    interval = str(raw.get("interval") or raw.get("salary_interval") or "yearly").lower()
    try:
        factor = {"hourly": 1950, "weekly": 52, "monthly": 12, "daily": 260}.get(interval, 1)
        low = float(minimum) * factor if minimum not in (None, "") else None
        high = float(maximum) * factor if maximum not in (None, "") else None
        if low is not None and high is not None:
            salary = "£{:,.0f}-£{:,.0f} per year".format(low, high)
        elif low is not None or high is not None:
            salary = f"£{(low if low is not None else high):,.0f} per year"
        else:
            salary = ""
        return low, high, salary
    except (TypeError, ValueError):
        return None, None, ""


def _gov_salary_amounts(salary: str) -> list[float]:
    values = [float(value.replace(",", "")) for value in re.findall(r"£\s*([\d,]+(?:\.\d+)?)", salary)]
    interval = salary.lower()
    factor = 1950 if re.search(r"per hour|an hour|hourly", interval) else 52 if re.search(r"per week|a week|weekly", interval) else 12 if re.search(r"per month|a month|monthly", interval) else 260 if re.search(r"per day|a day|daily", interval) else 1
    return [value * factor for value in values]


def _get_job_details(job: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(job["sourceUrl"], headers=REQUEST_HEADERS, timeout=(5, 12))
        response.raise_for_status()
    except requests.RequestException:
        return job
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.select_one("main") or soup
    description = main.get_text(" ", strip=True)
    if description:
        job["description"] = description[:12000]
    for anchor in soup.select("a[href]"):
        href = _safe_url(anchor.get("href", ""), job["sourceUrl"])
        if href and "apply" in anchor.get_text(" ", strip=True).lower() and not href.startswith(JOBS_BASE_URL + "/jobs/"):
            job["applicationUrl"] = href
            job["url"] = href
            break
    close_date = re.search(
        r"(?:advert|application|job)\s+(?:closes?|closing date)\s+(?:on\s+)?(?:\w+\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        description,
        re.IGNORECASE,
    )
    if close_date:
        for pattern in ("%d %b %Y", "%d %B %Y"):
            try:
                closing = datetime.strptime(close_date.group(1), pattern).replace(tzinfo=timezone.utc)
                job["closed"] = closing.date() < datetime.now(timezone.utc).date()
                break
            except ValueError:
                continue
    return job


def fetch_gov_jobs(query: str, location: str, radius: int, days: int = 28) -> tuple[list[dict[str, Any]], str]:
    if not query and not location:
        return [], ""
    params: dict[str, Any] = {"pageNumber": 1, "distance": radius}
    if query:
        params["keywords"] = correct_query(query)
    if location:
        params["location"] = location
    try:
        # The current Work Hub endpoint rejects the project-identifying
        # User-Agent with 403, while accepting a normal browser Accept header.
        # Keep the request header neutral so Government Jobs remains available.
        response = requests.get(
            JOBS_SEARCH_URL,
            params=params,
            headers={"Accept": "text/html,application/xhtml+xml,application/json"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        jobs = parse_gov_jobs(response.text)
    except requests.HTTPError as exc:
        response = exc.response
        code = response.status_code if response is not None else "unknown"
        return [], f"Government Jobs returned HTTP {code}"
    except requests.RequestException as exc:
        return [], f"Government Jobs request failed ({type(exc).__name__})"
    with ThreadPoolExecutor(max_workers=5) as pool:
        detailed = list(pool.map(_get_job_details, jobs[:RESULT_LIMIT]))
    current: list[dict[str, Any]] = []
    for detail in detailed:
        posted = datetime.fromisoformat(detail["date"].replace("Z", "+00:00"))
        if not detail.get("closed") and datetime.now(timezone.utc) - posted <= timedelta(days=days):
            current.append(detail)
    return current, ""


def search_jobspy(query: str, location: str, radius: int, days: int, salary_min: int | None, salary_max: int | None, job_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    from jobspy import scrape_jobs

    sites = [site.strip() for site in os.getenv("JOBSPY_SEARCH_SITES", "indeed,glassdoor").split(",") if site.strip()]
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    for term in search_terms(query):
        kwargs: dict[str, Any] = {
            "site_name": sites, "search_term": term, "results_wanted": RESULT_LIMIT,
            "hours_old": days * 24, "country_indeed": "UK", "distance": radius,
            "fetch_description": True, "verbose": 0,
        }
        if location:
            kwargs["location"] = location
        if job_type:
            kwargs["job_type"] = job_type
        try:
            frame = scrape_jobs(**kwargs)
        except Exception as exc:
            errors.append(type(exc).__name__)
            continue
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else []
        for row in rows:
            normalized, _ = normalize_job(row, datetime.now(timezone.utc), days)
            if not normalized:
                continue
            low, high, salary = _salary_value(row)
            if salary_min is not None and (high is None or high < salary_min):
                continue
            if salary_max is not None and (low is None or low > salary_max):
                continue
            normalized["salary"] = salary
            type_value = str(row.get("job_type") or "").lower().replace("_", " ").replace("-", " ")
            normalized["jobType"] = {
                "fulltime": "Full time", "full time": "Full time", "parttime": "Part time",
                "part time": "Part time", "contract": "Contract", "temporary": "Temporary",
                "internship": "Internship",
            }.get(type_value, type_value.title())
            jobs.append(normalized)
    return jobs, errors


def search_live_jobs(query: str, location: str, radius: int, days: int, salary_min: int | None, salary_max: int | None, job_type: str) -> dict[str, Any]:
    gov_jobs, gov_error = fetch_gov_jobs(query, location, radius, days)
    jobspy_jobs, jobspy_errors = search_jobspy(query, location, radius, days, salary_min, salary_max, job_type)
    combined: dict[str, dict[str, Any]] = {}
    for job in [*gov_jobs, *jobspy_jobs]:
        key = canonical_url(str(job.get("sourceUrl") or job.get("url") or ""))
        if not key:
            continue
        fingerprint = "|".join(
            re.sub(r"[^a-z0-9]+", "", str(job.get(field, "")).lower())
            for field in ("employer", "title", "location")
        ) + "|" + str(job.get("date", ""))[:10]
        key = key or fingerprint
        if fingerprint in {item.get("_fingerprint") for item in combined.values()}:
            continue
        job["_fingerprint"] = fingerprint
        if key not in combined or len(str(job.get("description", ""))) > len(str(combined[key].get("description", ""))):
            combined[key] = job
    register_names, register_url, register_count = _load_sponsor_register()
    results = [annotate_sponsorship(job, register_names) for job in combined.values()]
    for job in results:
        job["relevanceScore"] = relevance_score(job, query)
        if job.get("source") == "UK Government Jobs" and (salary_min is not None or salary_max is not None):
            amounts = _gov_salary_amounts(str(job.get("salary", "")))
            if not amounts or (salary_min is not None and max(amounts) < salary_min) or (salary_max is not None and min(amounts) > salary_max):
                job["_salaryRejected"] = True
    results = [job for job in results if not job.pop("_salaryRejected", False)]
    for job in results:
        job.pop("_fingerprint", None)
    results.sort(key=lambda job: (job["relevanceScore"], str(job.get("date", ""))), reverse=True)
    errors = []
    if gov_error:
        errors.append({"source": "UK Government Jobs", "error": gov_error})
    if jobspy_errors and len(jobspy_errors) == len(search_terms(query)):
        errors.append({"source": "JobSpy", "error": "All JobSpy searches failed"})
    return {
        "jobs": results, "sources": ["UK Government Jobs", "JobSpy"],
        "sponsorRegister": {"available": bool(register_names), "url": register_url, "rowCount": register_count},
        "errors": errors,
    }
