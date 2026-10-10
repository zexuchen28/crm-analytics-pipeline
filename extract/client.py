"""HTTP client for the CRM API: pagination plus retry with backoff.

Retry policy
- retried:      connection errors, timeouts, HTTP 429/500/502/503/504
- not retried:  every other non-200 (a 400/404 is a bug on our side; fail fast)
- delay:        the server's Retry-After if present (capped), else exponential
                backoff with a little jitter
- when retries run out an ExtractionError is raised. Callers must treat that as
  "this sync did not happen" and must not advance any state.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterator

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_SECONDS = 30.0


class ExtractionError(RuntimeError):
    """The extract could not be completed; nothing should be committed."""


class CRMClient:
    def __init__(self, base_url: str, page_size: int = 100, max_retries: int = 4,
                 backoff_base: float = 0.5, timeout: float = 10.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep = sleep

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return self.backoff_base * (2 ** attempt) + random.uniform(0, self.backoff_base / 2)

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            retry_after = None
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES:
                    raise ExtractionError(
                        f"GET {path} -> HTTP {resp.status_code} (not retryable): {resp.text[:200]}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")

            if attempt < self.max_retries:
                delay = self._delay(attempt, retry_after)
                log.warning("GET %s failed (%s); retry %d/%d in %.1fs",
                            path, last_error, attempt + 1, self.max_retries, delay)
                self.sleep(delay)

        raise ExtractionError(f"GET {path} failed after {self.max_retries + 1} attempts: {last_error}")

    def iter_pages(self, resource: str, updated_since: str | None = None) -> Iterator[list[dict]]:
        """Yield one list of records per page, following the 'after' cursor."""
        after = None
        while True:
            params: dict = {"limit": self.page_size}
            if updated_since:
                params["updated_since"] = updated_since
            if after:
                params["after"] = after
            body = self.get_json(f"/{resource}", params)
            yield body["results"]
            next_after = body["paging"]["next_after"]
            if not next_after:
                return
            if next_after == after:
                raise ExtractionError(f"{resource}: pagination cursor did not advance")
            after = next_after
