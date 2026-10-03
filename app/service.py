"""All business logic. Every function is ONE atomic transaction.

Life cycle of the units in an order (an order's state is the state of ALL its units)

    AVAILABLE --reserve--> RESERVED --pay--> PAYMENT_PENDING --payment ok--> CONFIRMED
                              |                    |                             |
                              |                    |                  cancel --> REFUND_PENDING --> REFUNDED
                              +--------------------+--> EXPIRED        (reservation time ran out)
                              +--------------------+--> CANCELLED      (customer cancelled)
                              +--------------------+--> PAYMENT_FAILED (payment failed)

EXPIRED, CANCELLED, PAYMENT_FAILED and REFUND_PENDING put the units back to AVAILABLE.
That happens at most once per order, because the state change is guarded.
"""
import threading
import time
import uuid

from . import config
from .db import tx, get_conn

LIVE = ("RESERVED", "PAYMENT_PENDING")                  # holding units, not paid yet
HOLDING = ("RESERVED", "PAYMENT_PENDING", "CONFIRMED")   # all states that use up stock
HOLDING_SQL = "('RESERVED','PAYMENT_PENDING','CONFIRMED')"

ALLOWED = {
    ("AVAILABLE", "RESERVED"),            # done by reserve()
    ("RESERVED", "PAYMENT_PENDING"),
    ("RESERVED", "CONFIRMED"),            # payment confirmed although we never saw the "pay" click
    ("PAYMENT_PENDING", "CONFIRMED"),
    ("RESERVED", "EXPIRED"),
    ("PAYMENT_PENDING", "EXPIRED"),
    ("RESERVED", "CANCELLED"),
    ("PAYMENT_PENDING", "CANCELLED"),
    ("RESERVED", "PAYMENT_FAILED"),
    ("PAYMENT_PENDING", "PAYMENT_FAILED"),
    ("CONFIRMED", "REFUND_PENDING"),      # customer cancels a paid order
    ("REFUND_PENDING", "REFUNDED"),       # money has gone back
}
RELEASES_STOCK = ("EXPIRED", "CANCELLED", "PAYMENT_FAILED", "REFUND_PENDING")

# ---- counters for "protections that fired" (kept in memory, reset on server restart)
_counters = {"replayed": 0, "duplicate_callbacks": 0}
_counters_lock = threading.Lock()


def _bump(name: str) -> None:
    with _counters_lock:
        _counters[name] += 1


def reset_counters() -> None:
    with _counters_lock:
        for k in _counters:
            _counters[k] = 0


class _StockChanged(Exception):
    """Raised inside a transaction if stock is not what we just read (should never happen)."""


def _now() -> float:
    return time.time()


def _load_order(c, order_id: str):
    r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["allocations"] = [
        dict(a)
        for a in c.execute(
            "SELECT warehouse_id, qty FROM allocations WHERE order_id=? ORDER BY warehouse_id", (order_id,)
        ).fetchall()
    ]
    return d


def _transition(c, order_id: str, to_state: str, reason: str, payment_ref=None) -> bool:
    """Move an order to a new state. Returns False if that move is not allowed.

    The UPDATE has 'AND state IN (...)', so it only succeeds if the order is
    still in a valid starting state. That is what makes giving stock back safe:
    it can happen at most once per order.
    """
    row = c.execute("SELECT state FROM orders WHERE id=?", (order_id,)).fetchone()
    if row is None:
        return False
    from_states = [f for (f, t) in ALLOWED if t == to_state and f != "AVAILABLE"]
    if row["state"] not in from_states:
        return False
    placeholders = ",".join("?" * len(from_states))
    res = c.execute(
        f"UPDATE orders SET state=?, updated_at=?, payment_ref=COALESCE(?, payment_ref) "
        f"WHERE id=? AND state IN ({placeholders})",
        (to_state, _now(), payment_ref, order_id, *from_states),
    )
    if res.rowcount != 1:
        return False
    if to_state in RELEASES_STOCK:
        for a in c.execute("SELECT warehouse_id, qty FROM allocations WHERE order_id=?", (order_id,)).fetchall():
            c.execute(
                "UPDATE warehouses SET available = available + ? WHERE id=?",
                (a["qty"], a["warehouse_id"]),
            )
        reason += " (units back to AVAILABLE)"
    c.execute(
        "INSERT INTO order_events(order_id, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
        (order_id, row["state"], to_state, reason, _now()),
    )
    return True


# ---------------------------------------------------------------- reserve
def _replay(conn, row, user_id: str, sku: str, quantity: int) -> dict:
    """The Idempotency-Key was used before. Same request -> same answer. Different request -> error."""
    if row["user_id"] != user_id or row["sku"] != sku or row["quantity"] != quantity:
        return {"result": "key_conflict", "order": None}
    _bump("replayed")
    return {"result": "replayed", "order": _load_order(conn, row["id"])}


def reserve(user_id: str, sku: str, idem_key: str, quantity: int = 1) -> dict:
    """Try to reserve `quantity` units, all or nothing. Units may come from several warehouses.

    Returns {"result": created | replayed | already_has_order | sold_out |
                       insufficient_stock | key_conflict | invalid_quantity, "order": ...}
    """
    if quantity < 1 or quantity > config.MAX_QTY:
        return {"result": "invalid_quantity", "order": None}

    # FAST PATH (read-only, no write lock). Most of the 10,000 requests are
    # retries or arrive after sold-out, so they should not queue for the lock.
    # Trade-off: if units are released at this very moment, this one request may
    # see a stale 'sold out'. That is safe (we never oversell) and the user can retry.
    rc = get_conn()
    seen = rc.execute("SELECT * FROM orders WHERE idempotency_key=?", (idem_key,)).fetchone()
    if seen:
        return _replay(rc, seen, user_id, sku, quantity)
    left = rc.execute("SELECT COALESCE(SUM(available),0) FROM warehouses WHERE sku=?", (sku,)).fetchone()[0]
    if left == 0:
        return {"result": "sold_out", "order": None, "available": 0}
    if left < quantity:
        return {"result": "insufficient_stock", "order": None, "available": left}

    try:
        with tx() as c:
            # 1) Same Idempotency-Key = a retry. It must come from the same user with the same request.
            existing = c.execute("SELECT * FROM orders WHERE idempotency_key=?", (idem_key,)).fetchone()
            if existing:
                return _replay(c, existing, user_id, sku, quantity)

            # 2) One live order per user per product
            mine = c.execute(
                f"SELECT id FROM orders WHERE user_id=? AND sku=? AND state IN {HOLDING_SQL}", (user_id, sku)
            ).fetchone()
            if mine:
                return {"result": "already_has_order", "order": _load_order(c, mine["id"])}

            # 3) Plan: take from the warehouse with the most stock first, then the next one...
            rows = c.execute(
                "SELECT id, available FROM warehouses WHERE sku=? AND available>0 ORDER BY available DESC, id",
                (sku,),
            ).fetchall()
            free = sum(r["available"] for r in rows)
            if free == 0:
                return {"result": "sold_out", "order": None, "available": 0}
            if free < quantity:                                   # all or nothing
                return {"result": "insufficient_stock", "order": None, "available": free}
            plan, remaining = [], quantity
            for r in rows:
                take = min(remaining, r["available"])
                plan.append((r["id"], take))
                remaining -= take
                if remaining == 0:
                    break

            # 4) Take the units. 'available >= take' in the WHERE clause is a second guard.
            for wid, take in plan:
                res = c.execute(
                    "UPDATE warehouses SET available = available - ? WHERE id=? AND available >= ?",
                    (take, wid, take),
                )
                if res.rowcount != 1:
                    raise _StockChanged()

            # 5) Create the order + its allocations in the same transaction as the stock change.
            now = _now()
            order_id = "ord_" + uuid.uuid4().hex[:12]
            c.execute(
                "INSERT INTO orders(id,user_id,sku,quantity,state,idempotency_key,created_at,expires_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, user_id, sku, quantity, "RESERVED", idem_key, now, now + config.RESERVATION_TTL, now),
            )
            for wid, take in plan:
                c.execute("INSERT INTO allocations(order_id, warehouse_id, qty) VALUES (?,?,?)", (order_id, wid, take))
            c.execute(
                "INSERT INTO order_events(order_id, from_state, to_state, reason, ts) VALUES (?,?,?,?,?)",
                (order_id, "AVAILABLE", "RESERVED", "reserved", now),
            )
            return {"result": "created", "order": _load_order(c, order_id)}
    except _StockChanged:
        return {"result": "insufficient_stock", "order": None, "available": 0}


# ---------------------------------------------------------------- payment
def start_payment(order_id: str) -> dict:
    """User clicks 'pay'. Moves RESERVED -> PAYMENT_PENDING and returns a payment reference."""
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            return {"result": "not_found"}
        if order["state"] == "PAYMENT_PENDING":      # retry of the same call
            return {"result": "ok", "payment_ref": order["payment_ref"], "order": _load_order(c, order_id)}
        if order["state"] != "RESERVED":
            return {"result": "invalid_state", "order": _load_order(c, order_id)}
        if _now() > order["expires_at"]:             # too late, even if the worker has not run yet
            _transition(c, order_id, "EXPIRED", "pay_after_deadline")
            return {"result": "expired"}
        ref = "pay_" + uuid.uuid4().hex[:12]
        _transition(c, order_id, "PAYMENT_PENDING", "payment_started", payment_ref=ref)
        return {"result": "ok", "payment_ref": ref, "order": _load_order(c, order_id)}


def handle_payment_callback(event_id: str, order_id: str, status: str) -> dict:
    """Webhook from the payment provider. Safe to receive any number of times.

    The 'processed_events' insert and the order update are in the SAME
    transaction. So after a crash, either both happened or neither did.
    """
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            return {"result": "not_found"}

        seen = c.execute(
            "INSERT OR IGNORE INTO processed_events(event_id, order_id, received_at) VALUES (?,?,?)",
            (event_id, order_id, _now()),
        )
        if seen.rowcount == 0:
            _bump("duplicate_callbacks")
            return {"result": "duplicate_ignored"}

        if status == "success":
            if order["state"] in LIVE:
                _transition(c, order_id, "CONFIRMED", "payment_success")
                return {"result": "confirmed"}
            if order["state"] == "CONFIRMED":
                return {"result": "already_confirmed"}
            # Money arrived but the order is not live (expired, cancelled, failed, refunded):
            # never sell stock that was given back. Queue a refund instead.
            c.execute(
                "INSERT INTO refunds(order_id,event_id,reason,status,created_at) VALUES (?,?,?,'PENDING',?)",
                (order_id, event_id, f"payment_after_{order['state'].lower()}", _now()),
            )
            return {"result": "late_payment_refund_queued"}

        # status == "failed"
        if order["state"] in LIVE:
            _transition(c, order_id, "PAYMENT_FAILED", "payment_failed")
            return {"result": "payment_failed_stock_released"}
        return {"result": "ignored"}


def cancel(order_id: str) -> dict:
    """Customer cancels. Before payment: units go back. After payment: units go back AND a refund starts."""
    with tx() as c:
        order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            return {"result": "not_found"}
        state = order["state"]
        if state in ("CANCELLED", "REFUND_PENDING", "REFUNDED"):     # already cancelled: same answer again
            return {"result": "ok", "order": _load_order(c, order_id)}
        if state in LIVE:
            _transition(c, order_id, "CANCELLED", "user_cancelled")
            return {"result": "ok", "order": _load_order(c, order_id)}
        if state == "CONFIRMED":
            _transition(c, order_id, "REFUND_PENDING", "cancelled_after_payment")
            c.execute(
                "INSERT INTO refunds(order_id,event_id,reason,status,created_at) VALUES (?,?,?,'PENDING',?)",
                (order_id, "cancel-" + order_id, "cancelled_after_payment", _now()),
            )
            return {"result": "ok", "refund": "pending", "order": _load_order(c, order_id)}
        return {"result": "invalid_state", "order": _load_order(c, order_id)}   # EXPIRED / PAYMENT_FAILED


# ---------------------------------------------------------------- background jobs
def expire_stale() -> int:
    """Release every reservation whose time is up. Returns how many were released."""
    now = _now()
    # cheap read first, so the worker does not take the write lock when there is nothing to do
    if get_conn().execute(
        "SELECT 1 FROM orders WHERE state IN ('RESERVED','PAYMENT_PENDING') AND expires_at <= ? LIMIT 1", (now,)
    ).fetchone() is None:
        return 0
    with tx() as c:
        rows = c.execute(
            "SELECT id FROM orders WHERE state IN ('RESERVED','PAYMENT_PENDING') AND expires_at <= ?", (now,)
        ).fetchall()
        n = 0
        for r in rows:
            if _transition(c, r["id"], "EXPIRED", "reservation_timeout"):
                n += 1
        return n


def process_refunds() -> int:
    """The (simulated) payment provider finishes refunds that are old enough. Returns how many."""
    cutoff = _now() - config.REFUND_DELAY
    if get_conn().execute(
        "SELECT 1 FROM refunds WHERE status='PENDING' AND created_at <= ? LIMIT 1", (cutoff,)
    ).fetchone() is None:
        return 0
    with tx() as c:
        rows = c.execute(
            "SELECT id, order_id FROM refunds WHERE status='PENDING' AND created_at <= ?", (cutoff,)
        ).fetchall()
        n = 0
        for r in rows:
            res = c.execute(
                "UPDATE refunds SET status='REFUNDED', processed_at=? WHERE id=? AND status='PENDING'",
                (_now(), r["id"]),
            )
            if res.rowcount == 1:
                # only orders that were cancelled after payment are in REFUND_PENDING;
                # for a late payment the order stays EXPIRED / CANCELLED and this does nothing
                _transition(c, r["order_id"], "REFUNDED", "refund_processed")
                n += 1
        return n


# ---------------------------------------------------------------- reads / checks
def get_order(order_id: str):
    return _load_order(get_conn(), order_id)


def order_history(order_id: str):
    rows = get_conn().execute(
        "SELECT from_state, to_state, reason, ts FROM order_events WHERE order_id=? ORDER BY id", (order_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def inventory() -> dict:
    c = get_conn()
    wh = c.execute("SELECT id, total, available FROM warehouses ORDER BY id").fetchall()
    orders_by_state = {}
    units_by_state = {"AVAILABLE": sum(r["available"] for r in wh), "RESERVED": 0, "PAYMENT_PENDING": 0, "CONFIRMED": 0}
    for r in c.execute("SELECT state, COUNT(*) AS n, SUM(quantity) AS q FROM orders GROUP BY state").fetchall():
        orders_by_state[r["state"]] = r["n"]
        if r["state"] in HOLDING:
            units_by_state[r["state"]] = r["q"]
    return {
        "warehouses": [dict(r) for r in wh],
        "total_units": sum(r["total"] for r in wh),
        "available_units": units_by_state["AVAILABLE"],
        "units_by_state": units_by_state,
        "orders_by_state": orders_by_state,
    }


def metrics() -> dict:
    """How many times each protection fired.
    replayed + duplicate callbacks come from the counters above.
    refunds + expired are counted from the database, so they survive a restart."""
    c = get_conn()
    expired = c.execute("SELECT COUNT(*) FROM orders WHERE state='EXPIRED'").fetchone()[0]
    late = c.execute("SELECT COUNT(*) FROM refunds WHERE reason LIKE 'payment_after_%'").fetchone()[0]
    with _counters_lock:
        snap = dict(_counters)
    return {
        "replayed_requests": snap["replayed"],
        "duplicate_callbacks_blocked": snap["duplicate_callbacks"],
        "late_payments_refunded": late,
        "reservations_expired": expired,
    }


def _held_units(c, warehouse_id: str) -> int:
    return c.execute(
        f"SELECT COALESCE(SUM(a.qty),0) FROM allocations a JOIN orders o ON o.id=a.order_id "
        f"WHERE a.warehouse_id=? AND o.state IN {HOLDING_SQL}",
        (warehouse_id,),
    ).fetchone()[0]


def check_invariants() -> dict:
    """The rules that must ALWAYS be true:
         for each warehouse:  available + units held by live/confirmed orders == total
         every order has exactly as many allocated units as it asked for
         confirmed units never exceed the total stock."""
    c = get_conn()
    problems = []
    stock_total = 0
    for w in c.execute("SELECT id, total, available FROM warehouses").fetchall():
        held = _held_units(c, w["id"])
        stock_total += w["total"]
        if w["available"] < 0 or w["available"] + held != w["total"]:
            problems.append(f"{w['id']}: available={w['available']} held={held} total={w['total']}")
    for r in c.execute(
        "SELECT o.id, o.quantity, COALESCE(SUM(a.qty),0) AS got FROM orders o "
        "LEFT JOIN allocations a ON a.order_id=o.id GROUP BY o.id HAVING got != o.quantity"
    ).fetchall():
        problems.append(f"order {r['id']} asked for {r['quantity']} but has {r['got']} allocated")
    confirmed_units = c.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE state='CONFIRMED'").fetchone()[0]
    confirmed_orders = c.execute("SELECT COUNT(*) FROM orders WHERE state='CONFIRMED'").fetchone()[0]
    if confirmed_units > stock_total:
        problems.append(f"OVERSOLD: confirmed={confirmed_units} > stock={stock_total}")
    return {
        "ok": not problems,
        "problems": problems,
        "confirmed": confirmed_units,
        "confirmed_orders": confirmed_orders,
        "stock": stock_total,
    }


def reconcile() -> list:
    """Run at startup. Recomputes 'available' from the orders and allocations (the source
    of truth) and fixes any drift. A correct system should find nothing to fix."""
    fixes = []
    with tx() as c:
        for w in c.execute("SELECT id, total, available FROM warehouses").fetchall():
            correct = w["total"] - _held_units(c, w["id"])
            if correct != w["available"]:
                c.execute("UPDATE warehouses SET available=? WHERE id=?", (correct, w["id"]))
                fixes.append({"warehouse": w["id"], "was": w["available"], "now": correct})
    return fixes