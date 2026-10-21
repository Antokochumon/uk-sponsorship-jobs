"""Authenticated imports and read-after-write verification for Apps Script."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

import requests

from collect_jobs import canonical_url


class AppsScriptImportError(RuntimeError):
    pass


class AppsScriptClient:
    def __init__(self, import_url: str, read_url: str, token: str, timeout: tuple[int, int] = (10, 60)) -> None:
        self.import_url = import_url.strip()
        self.read_url = read_url.strip()
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self._is_https_url(self.import_url) and self._is_https_url(self.read_url))

    @staticmethod
    def _is_https_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            return parsed.scheme.lower() == "https" and bool(parsed.hostname)
        except ValueError:
            return False

    def import_batch(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.configured:
            raise AppsScriptImportError("HTTPS Apps Script URLs and a token are required.")
        if not jobs:
            return {"counts": {"received": 0, "inserted": 0, "updated": 0, "duplicates": 0, "rejected": 0, "imported": 0}, "acceptedUrls": []}
        try:
            response = requests.post(
                self.import_url,
                json={"action": "importJobs", "token": self.token, "jobs": jobs},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f", HTTP {status}" if status else ""
            raise AppsScriptImportError(f"Apps Script import request failed ({type(exc).__name__}{suffix}).") from None

        try:
            payload = response.json()
        except ValueError:
            raise AppsScriptImportError("Apps Script import response was not JSON; no success was assumed.") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = payload.get("error", "response did not confirm success") if isinstance(payload, dict) else "invalid response object"
            raise AppsScriptImportError(f"Apps Script rejected the batch: {str(detail)[:240]}")

        counts = payload.get("counts")
        if not isinstance(counts, dict):
            raise AppsScriptImportError("Apps Script response omitted import counts.")
        normalized_counts: dict[str, int] = {}
        for name in ("received", "inserted", "updated", "duplicates", "rejected", "imported"):
            value = counts.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AppsScriptImportError(f"Apps Script returned an invalid {name} count.")
            normalized_counts[name] = value
        if normalized_counts["received"] != len(jobs):
            raise AppsScriptImportError("Apps Script received count did not match the submitted batch.")
        if normalized_counts["imported"] != normalized_counts["inserted"] + normalized_counts["updated"]:
            raise AppsScriptImportError("Apps Script imported count did not match inserted plus updated.")
        if sum(normalized_counts[name] for name in ("inserted", "updated", "duplicates", "rejected")) != len(jobs):
            raise AppsScriptImportError("Apps Script result counts did not account for every submitted record.")

        accepted_urls = payload.get("acceptedUrls")
        if not isinstance(accepted_urls, list) or not all(isinstance(url, str) for url in accepted_urls):
            raise AppsScriptImportError("Apps Script response omitted accepted application URLs for read-back verification.")
        unique_accepted = {canonical_url(url) for url in accepted_urls if canonical_url(url)}
        if len(unique_accepted) < normalized_counts["imported"]:
            raise AppsScriptImportError("Apps Script returned fewer accepted URLs than inserted/updated rows.")
        return {"counts": normalized_counts, "acceptedUrls": accepted_urls}

    def read_jobs(self) -> list[dict[str, Any]]:
        if not self.read_url:
            raise AppsScriptImportError("Apps Script read URL is not configured.")
        try:
            response = requests.get(
                self.read_url,
                params={"_": int(time.time() * 1000)},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f", HTTP {status}" if status else ""
            raise AppsScriptImportError(f"Apps Script read-back failed ({type(exc).__name__}{suffix}).") from None
        try:
            payload = response.json()
        except ValueError:
            raise AppsScriptImportError("Apps Script read-back was not JSON.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise AppsScriptImportError("Apps Script read-back did not contain a jobs array.")
        return payload["jobs"]

    def admin_action(self, action: str, **payload: Any) -> dict[str, Any]:
        """Run a token-protected Apps Script maintenance action."""
        if not self.configured:
            raise AppsScriptImportError("HTTPS Apps Script URLs and a token are required.")
        body = {"action": action, "token": self.token, **payload}
        try:
            response = requests.post(self.import_url, json=body, headers={"Accept": "application/json"}, timeout=self.timeout)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            raise AppsScriptImportError(f"Apps Script maintenance request failed ({type(exc).__name__}).") from None
        except ValueError:
            raise AppsScriptImportError("Apps Script maintenance response was not JSON.") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            detail = result.get("error", "response did not confirm success") if isinstance(result, dict) else "invalid response object"
            raise AppsScriptImportError(f"Apps Script maintenance action failed: {str(detail)[:240]}")
        return result

    def test_one_real_job(self, job: dict[str, Any]) -> dict[str, Any]:
        expected_url = canonical_url(job.get("applicationUrl") or job.get("url"))
        if not expected_url:
            raise AppsScriptImportError("The real test listing has no valid application URL.")
        result = self.import_batch([job])
        verification = self.verify_saved_urls([expected_url], result["counts"])
        accounted = result["counts"]["imported"] + result["counts"]["duplicates"] == 1
        verification["confirmed"] = verification["confirmed"] and accounted and result["counts"]["rejected"] == 0
        if not accounted or result["counts"]["rejected"]:
            verification["errors"].append({"error": "The real test listing was rejected or not accounted for as inserted, updated, or duplicate."})
        return verification

    def import_jobs(self, jobs: list[dict[str, Any]], batch_size: int = 50) -> dict[str, Any]:
        totals = {"submitted": len(jobs), "received": 0, "inserted": 0, "updated": 0, "duplicates": 0, "rejected": 0, "imported": 0}
        accepted_urls: list[str] = []
        errors: list[dict[str, Any]] = []
        batches = [jobs[index:index + batch_size] for index in range(0, len(jobs), batch_size)]

        for batch_number, batch in enumerate(batches, start=1):
            try:
                result = self.import_batch(batch)
            except AppsScriptImportError as exc:
                errors.append({"batch": batch_number, "error": str(exc)})
                break
            totals["received"] += result["counts"]["received"]
            for name in ("inserted", "updated", "duplicates", "rejected", "imported"):
                totals[name] += result["counts"][name]
            accepted_urls.extend(result["acceptedUrls"])

        verification = self.verify_saved_urls(accepted_urls, totals, errors=errors)
        verification["batches"] = len(batches)
        return verification

    def verify_saved_urls(
        self,
        accepted_urls: list[str],
        counts: dict[str, int],
        errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        unique_keys = {canonical_url(url) for url in accepted_urls if canonical_url(url)}
        verification_errors = list(errors or [])
        try:
            saved_keys = {
                canonical_url(job.get("url") or job.get("Application URL"))
                for job in self.read_jobs()
                if isinstance(job, dict)
            }
        except AppsScriptImportError as exc:
            saved_keys = set()
            verification_errors.append({"error": str(exc)})
        verified = len(unique_keys & saved_keys)
        missing = len(unique_keys) - verified
        if missing:
            verification_errors.append({"error": f"Read-back did not find {missing} accepted application URL(s) in the Jobs feed."})
        return {
            "confirmed": bool(unique_keys) and not verification_errors and missing == 0,
            "counts": counts,
            "verifiedSaved": verified,
            "acceptedForVerification": len(unique_keys),
            "errors": verification_errors,
        }
