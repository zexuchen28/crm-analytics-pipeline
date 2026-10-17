"""In-memory CRM store plus the API semantics (filtering, keyset pagination,
failure injection).

This module deliberately has no web-framework imports. The FastAPI app in
main.py is a thin wrapper around `CRMStore.api_list`, and the tests drive the
same code through a tiny stdlib HTTP server. One implementation, two front ends.
"""
from __future__ import annotations

import base64
import json
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RESOURCES = ("accounts", "contacts", "deals", "campaigns", "usage")
STAGES = ["Lead", "Discovery", "Demo", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
CLOSED_STAGES = {"Closed Won", "Closed Lost"}
BUYING_MOTIONS = ("professor_led", "instructional_designer_led")

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "crm.json"
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"  # second precision, UTC; sorts lexicographically


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------------
@dataclass
class FailureInjector:
    """Makes the API misbehave on demand so retry/safe-rerun logic can be shown.

    fail_on_page: fail requests for this page number (1-based) of a scan
    fail_times:   how many times that page fails before it starts succeeding
                  (use a large number to simulate a hard outage)
    fail_rate:    additionally fail this fraction of all requests at random
    status:       HTTP status to return (503 by default; 429 adds Retry-After)
    """

    fail_on_page: int | None = None
    fail_times: int = 1
    fail_rate: float = 0.0
    status: int = 503
    _remaining: int = 0
    _scan: dict = field(default_factory=dict)
    _rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self._remaining = self.fail_times if self.fail_on_page else 0

    @classmethod
    def from_env(cls) -> "FailureInjector":
        page = os.getenv("FAIL_ON_PAGE")
        return cls(
            fail_on_page=int(page) if page else None,
            fail_times=int(os.getenv("FAIL_TIMES", "1")),
            fail_rate=float(os.getenv("FAIL_RATE", "0")),
            status=int(os.getenv("FAIL_STATUS", "503")),
        )

    def configure(self, fail_on_page=None, fail_times=1, fail_rate=0.0, status=503) -> None:
        self.fail_on_page = fail_on_page
        self.fail_times = fail_times
        self.fail_rate = fail_rate
        self.status = status
        self._remaining = fail_times if fail_on_page else 0
        self._scan = {}

    def _page_number(self, resource: str, after: str | None) -> int:
        """Which page of the current scan is this request? A request without a
        cursor starts a scan; a repeat of the same cursor is a retry, not a new page."""
        state = self._scan.setdefault(resource, {"page": 0, "last_after": None})
        if after is None:
            state["page"] = 1
        elif after != state["last_after"]:
            state["page"] += 1
        state["last_after"] = after
        return state["page"]

    def check(self, resource: str, after: str | None) -> tuple[int, dict] | None:
        page = self._page_number(resource, after)
        hit = False
        if self.fail_on_page == page and self._remaining > 0:
            self._remaining -= 1
            hit = True
        elif self.fail_rate and self._rng.random() < self.fail_rate:
            hit = True
        if not hit:
            return None
        headers = {"Retry-After": "1"} if self.status == 429 else {}
        return self.status, headers


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
@dataclass
class ApiResult:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)


def _cursor(record: dict) -> str:
    return base64.urlsafe_b64encode(f"{record['updated_at']}|{record['id']}".encode()).decode()


def _decode_cursor(token: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        ts, _, rid = raw.partition("|")
        parse_ts(ts)
        if not rid:
            raise ValueError("empty id")
        return ts, rid
    except Exception as exc:  # any malformed token is a client error
        raise ValueError("invalid 'after' cursor") from exc


class CRMStore:
    def __init__(self, data: dict[str, list[dict]], injector: FailureInjector | None = None,
                 path: Path | None = None):
        self.data = {name: list(data.get(name, [])) for name in RESOURCES}
        self.injector = injector or FailureInjector()
        self.path = path
        self._lock = threading.RLock()

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str = DEFAULT_DATA_PATH, injector: FailureInjector | None = None):
        path = Path(path)
        with path.open() as fh:
            return cls(json.load(fh), injector=injector, path=path)

    def save(self, path: Path | str | None = None) -> None:
        target = Path(path or self.path or DEFAULT_DATA_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, target.open("w") as fh:
            json.dump(self.data, fh)

    # -- reading -----------------------------------------------------------
    def list_page(self, resource: str, updated_since: str | None = None,
                  after: str | None = None, limit: int = DEFAULT_LIMIT):
        """Keyset pagination over (updated_at, id). `updated_since` is inclusive.
        Keyset (not offset) paging means records edited mid-scan are not skipped."""
        with self._lock:
            ordered = sorted(self.data[resource], key=lambda r: (r["updated_at"], r["id"]))
            if updated_since:
                parse_ts(updated_since)  # validates the format
                ordered = [r for r in ordered if r["updated_at"] >= updated_since]
            if after:
                key = _decode_cursor(after)
                ordered = [r for r in ordered if (r["updated_at"], r["id"]) > key]
            page = [dict(r) for r in ordered[:limit]]
            has_more = len(ordered) > limit
            return page, (_cursor(page[-1]) if has_more else None)

    def api_list(self, resource: str, updated_since: str | None = None,
                 after: str | None = None, limit: int = DEFAULT_LIMIT) -> ApiResult:
        if resource not in RESOURCES:
            return ApiResult(404, {"error": f"unknown resource '{resource}'"})
        if not 1 <= limit <= MAX_LIMIT:
            return ApiResult(400, {"error": f"limit must be between 1 and {MAX_LIMIT}"})
        with self._lock:
            failure = self.injector.check(resource, after)
            if failure:
                status, headers = failure
                return ApiResult(status, {"error": "injected failure"}, headers)
            try:
                rows, next_after = self.list_page(resource, updated_since, after, limit)
            except ValueError as exc:
                return ApiResult(400, {"error": str(exc)})
        return ApiResult(200, {"results": rows, "paging": {"next_after": next_after}})

    def stats(self) -> dict:
        with self._lock:
            return {
                name: {"count": len(rows), "max_updated_at": max((r["updated_at"] for r in rows), default=None)}
                for name, rows in self.data.items()
            }

    # -- simulating a live CRM ---------------------------------------------
    def mutate(self, n_updates: int = 10, n_new: int = 2, now: datetime | None = None,
               seed: int | None = None) -> dict:
        """Advance some open deals one stage and add a few new ones, stamping
        updated_at = now. Lets you demo that the next sync pulls only the changes."""
        rng = random.Random(seed)
        now_s = fmt_ts(now or utcnow())
        with self._lock:
            deals = self.data["deals"]
            open_deals = [d for d in deals if d["stage"] not in CLOSED_STAGES]
            updated_ids = []
            for deal in rng.sample(open_deals, min(n_updates, len(open_deals))):
                if deal["stage"] == "Negotiation":
                    new_stage = "Closed Won" if rng.random() < 0.6 else "Closed Lost"
                else:
                    new_stage = STAGES[STAGES.index(deal["stage"]) + 1]
                deal["stage"] = new_stage
                deal["stage_history"].append({"stage": new_stage, "entered_at": now_s})
                if new_stage in CLOSED_STAGES:
                    deal["close_date"] = now_s[:10]
                if rng.random() < 0.3:
                    deal["amount"] = round(deal["amount"] * rng.uniform(0.85, 1.2), -2)
                deal["updated_at"] = now_s
                updated_ids.append(deal["id"])

            created_ids = []
            next_num = max((int(d["id"].split("_")[1]) for d in deals), default=0) + 1
            accounts, campaigns = self.data["accounts"], self.data["campaigns"]
            for _ in range(n_new if accounts else 0):
                account = rng.choice(accounts)
                motion = rng.choice(BUYING_MOTIONS)
                deal_id = f"deal_{next_num:04d}"
                next_num += 1
                deals.append({
                    "id": deal_id,
                    "account_id": account["id"],
                    "campaign_id": rng.choice(campaigns)["id"] if campaigns else None,
                    "name": f"{account['name']} - {motion}",
                    "buying_motion": motion,
                    "stage": "Lead",
                    "amount": float(rng.randint(5, 80) * 1000),
                    "created_at": now_s,
                    "close_date": None,
                    "stage_history": [{"stage": "Lead", "entered_at": now_s}],
                    "updated_at": now_s,
                })
                created_ids.append(deal_id)
            if self.path:
                self.save()
        return {"updated_ids": updated_ids, "created_ids": created_ids, "stamped_at": now_s}

    def delete_records(self, resource: str = "deals", n: int = 2, seed: int | None = None) -> dict:
        """Hard-delete n random records, as if someone deleted rows in the CRM.
        Deleted rows just stop appearing in the API, so an incremental sync can never see it.
        Only a reconciliation against the full source can."""
        if resource not in RESOURCES:
            raise ValueError(f"unknown resource '{resource}'")
        rng = random.Random(seed)
        with self._lock:
            rows = self.data[resource]
            doomed = {r["id"] for r in rng.sample(rows, min(n, len(rows)))}
            self.data[resource] = [r for r in rows if r["id"] not in doomed]
            if self.path:
                self.save()
        return {"deleted_ids": sorted(doomed)}
