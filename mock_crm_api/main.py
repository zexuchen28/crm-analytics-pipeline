"""FastAPI front end for the mock CRM.

    uvicorn mock_crm_api.main:app --port 8000      (interactive docs at /docs)

All behavior lives in store.py; this file only translates HTTP <-> CRMStore.
Failure injection can be preset with env vars (FAIL_ON_PAGE, FAIL_TIMES,
FAIL_RATE, FAIL_STATUS) or changed at runtime via POST /admin/failures.
"""
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .store import DEFAULT_DATA_PATH, DEFAULT_LIMIT, CRMStore, FailureInjector

app = FastAPI(title="Mock CRM API", version="0.1.0")

injector = FailureInjector.from_env()
store = CRMStore.load(Path(os.getenv("CRM_DATA_PATH", str(DEFAULT_DATA_PATH))), injector=injector)


class FailureConfig(BaseModel):
    fail_on_page: Optional[int] = None
    fail_times: int = 1
    fail_rate: float = 0.0
    status: int = 503


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/admin/stats")
def stats():
    return store.stats()


@app.post("/admin/mutate")
def mutate(n_updates: int = 10, n_new: int = 2):
    """Advance some open deals a stage and add new ones (simulates CRM activity)."""
    return store.mutate(n_updates=n_updates, n_new=n_new)


@app.post("/admin/delete")
def delete(resource: str = "deals", n: int = 2):
    """Hard-delete n random records (simulates deletions in the CRM)."""
    try:
        return store.delete_records(resource=resource, n=n)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@app.post("/admin/failures")
def set_failures(config: FailureConfig):
    """Configure failure injection. POST {} to clear it."""
    injector.configure(**config.model_dump())
    return {"failure_injection": config.model_dump()}


@app.get("/{resource}")
def list_resource(resource: str, updated_since: Optional[str] = None,
                  after: Optional[str] = None, limit: int = DEFAULT_LIMIT):
    result = store.api_list(resource, updated_since=updated_since, after=after, limit=limit)
    return JSONResponse(status_code=result.status, content=result.body, headers=result.headers)
