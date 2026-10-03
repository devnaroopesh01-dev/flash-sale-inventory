"""HTTP API (FastAPI). Thin layer: validate input, call service, return JSON."""
import collections
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db, service

STATIC_DIR = Path(__file__).parent / "static"

# ---- response-time meter: (finished_at, milliseconds) for every sale request
_perf = collections.deque(maxlen=200_000)
_perf_lock = threading.Lock()


class TimingMiddleware:
    """Measures how long the server takes to answer each POST to /orders or /payments."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not scope["path"].startswith(("/orders", "/payments"))
        ):
            await self.app(scope, receive, send)
            return
        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            with _perf_lock:
                _perf.append((time.time(), ms))


def _pct(sorted_vals, p):
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def _worker_loop():
    """Background job: give back expired reservations and finish pending refunds."""
    while True:
        try:
            service.expire_stale()
            service.process_refunds()
        except Exception as e:  # keep the worker alive no matter what
            print("background worker error:", e)
        time.sleep(config.EXPIRY_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup / crash recovery ----
    db.init_db()
    db.seed_if_empty()
    released = service.expire_stale()
    refunded = service.process_refunds()
    fixes = service.reconcile()
    print(f"[recovery] expired={released} refunds_finished={refunded} reconcile_fixes={fixes}")
    threading.Thread(target=_worker_loop, daemon=True).start()
    yield


app = FastAPI(title="Flash Sale Inventory", lifespan=lifespan)
app.add_middleware(TimingMiddleware)


@app.get("/", include_in_schema=False)
def home():
    """The website."""
    return FileResponse(STATIC_DIR / "index.html")


class BuyRequest(BaseModel):
    user_id: str
    sku: str = config.SKU
    quantity: int = Field(default=1, ge=1, le=config.MAX_QTY)


class CallbackRequest(BaseModel):
    event_id: str       # unique id from the payment provider
    order_id: str
    status: str         # "success" or "failed"


@app.post("/orders")
def create_order(body: BuyRequest, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    r = service.reserve(body.user_id, body.sku, idempotency_key, body.quantity)
    code = {
        "created": 201, "replayed": 200, "already_has_order": 409, "sold_out": 409,
        "insufficient_stock": 409, "key_conflict": 422, "invalid_quantity": 422,
    }[r["result"]]
    return JSONResponse(r, status_code=code)


@app.post("/orders/{order_id}/pay")
def pay(order_id: str):
    r = service.start_payment(order_id)
    code = {"ok": 200, "not_found": 404, "invalid_state": 409, "expired": 410}[r["result"]]
    return JSONResponse(r, status_code=code)


@app.post("/payments/callback")
def payment_callback(body: CallbackRequest):
    if body.status not in ("success", "failed"):
        raise HTTPException(400, "status must be success or failed")
    r = service.handle_payment_callback(body.event_id, body.order_id, body.status)
    return JSONResponse(r, status_code=404 if r["result"] == "not_found" else 200)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str):
    r = service.cancel(order_id)
    code = {"ok": 200, "not_found": 404, "invalid_state": 409}[r["result"]]
    return JSONResponse(r, status_code=code)


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    o = service.get_order(order_id)
    if o is None:
        raise HTTPException(404, "order not found")
    return {"order": o, "history": service.order_history(order_id)}


@app.get("/inventory")
def inventory():
    return service.inventory()


@app.get("/config")
def get_config():
    return {"reservation_ttl": config.RESERVATION_TTL, "sku": config.SKU, "max_quantity": config.MAX_QTY}


@app.get("/admin/invariants")
def invariants():
    return service.check_invariants()


@app.get("/admin/metrics")
def metrics():
    """How many times each protection fired."""
    return service.metrics()


@app.get("/admin/perf")
def perf(since: float = 0.0):
    """Response-time numbers for requests that finished after `since` (server clock).
    Call once without `since` to get the server's current time, then use it as the start mark."""
    with _perf_lock:
        values = [ms for (ts, ms) in _perf if ts >= since]
    values.sort()
    n = len(values)
    return {
        "now": time.time(),
        "count": n,
        "p50_ms": round(_pct(values, 50), 1),
        "p95_ms": round(_pct(values, 95), 1),
        "p99_ms": round(_pct(values, 99), 1),
        "max_ms": round(values[-1], 1) if n else 0.0,
        "avg_ms": round(sum(values) / n, 1) if n else 0.0,
    }


@app.post("/admin/expire")
def expire_now():
    return {"expired": service.expire_stale()}


# ---- demo-only endpoints (need FAULT_INJECTION=1) ----
@app.post("/admin/reset")
def reset():
    if not config.FAULT_INJECTION:
        raise HTTPException(403, "enable FAULT_INJECTION=1")
    db.reset_all()
    service.reset_counters()
    with _perf_lock:
        _perf.clear()
    return {"ok": True}


@app.post("/admin/crash")
def crash():
    """Kill the process instantly, like a power cut. No cleanup, no goodbye."""
    if not config.FAULT_INJECTION:
        raise HTTPException(403, "enable FAULT_INJECTION=1")
    os._exit(1)