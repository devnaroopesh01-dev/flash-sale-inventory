import time
from concurrent.futures import ThreadPoolExecutor

from app import config, db, service


def buy(i, key=None):
    return service.reserve(f"user{i}", config.SKU, key or f"key-{i}")


def test_never_oversell_under_concurrency():
    """500 buyers, 100 units. Exactly 100 must win."""
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = list(pool.map(buy, range(500)))
    created = [r for r in results if r["result"] == "created"]
    sold_out = [r for r in results if r["result"] == "sold_out"]
    assert len(created) == 100
    assert len(sold_out) == 400
    assert service.check_invariants()["ok"]
    assert service.inventory()["available_units"] == 0


def test_idempotent_retry_returns_same_order():
    a = buy(1, "same-key")
    b = buy(1, "same-key")
    assert a["result"] == "created"
    assert b["result"] == "replayed"
    assert a["order"]["id"] == b["order"]["id"]
    assert service.inventory()["available_units"] == 99  # only ONE unit taken


def test_one_live_order_per_user():
    a = service.reserve("alice", config.SKU, "k1")
    b = service.reserve("alice", config.SKU, "k2")  # new key, same user
    assert a["result"] == "created"
    assert b["result"] == "already_has_order"
    assert service.inventory()["available_units"] == 99


def test_duplicate_payment_callback_is_ignored():
    o = buy(1)["order"]
    service.start_payment(o["id"])
    r1 = service.handle_payment_callback("evt-1", o["id"], "success")
    r2 = service.handle_payment_callback("evt-1", o["id"], "success")
    assert r1["result"] == "confirmed"
    assert r2["result"] == "duplicate_ignored"
    assert service.get_order(o["id"])["state"] == "CONFIRMED"
    assert service.check_invariants()["ok"]


def test_failed_payment_releases_stock_once():
    o = buy(1)["order"]
    service.start_payment(o["id"])
    service.handle_payment_callback("evt-f1", o["id"], "failed")
    service.handle_payment_callback("evt-f2", o["id"], "failed")  # a second, different event
    assert service.get_order(o["id"])["state"] == "CANCELLED"
    assert service.inventory()["available_units"] == 100       # not 101
    assert service.check_invariants()["ok"]


def test_expiry_releases_stock(monkeypatch):
    o = buy(1)["order"]
    assert service.inventory()["available_units"] == 99
    monkeypatch.setattr(service, "_now", lambda: time.time() + 3600)  # jump 1 hour ahead
    assert service.expire_stale() == 1
    assert service.get_order(o["id"])["state"] == "EXPIRED"
    assert service.inventory()["available_units"] == 100
    assert service.expire_stale() == 0                          # second run does nothing
    assert service.check_invariants()["ok"]


def test_payment_after_expiry_goes_to_refund():
    o = buy(1)["order"]
    service.start_payment(o["id"])
    with db.tx() as c:  # force the order to be expired
        service._transition(c, o["id"], "EXPIRED", "test")
    r = service.handle_payment_callback("late-1", o["id"], "success")
    assert r["result"] == "late_payment_refund_queued"
    assert service.get_order(o["id"])["state"] == "EXPIRED"
    assert service.check_invariants()["ok"]


def test_cancel_then_released_unit_can_be_bought():
    for i in range(100):
        buy(i)
    assert buy(999)["result"] == "sold_out"
    o = service.reserve("user0", config.SKU, "key-0")["order"]
    service.cancel(o["id"])
    assert buy(999)["result"] == "created"
    assert service.check_invariants()["ok"]


def test_reconcile_fixes_drift():
    buy(1)
    with db.tx() as c:  # simulate corrupted counter
        c.execute("UPDATE warehouses SET available = available + 1 WHERE id='WH-1'")
    assert not service.check_invariants()["ok"]
    fixes = service.reconcile()
    assert len(fixes) == 1
    assert service.check_invariants()["ok"]


def test_concurrent_confirm_and_expire_race():
    """Payment success and expiry hit the same order at the same moment.
    Whatever wins, stock must stay correct."""
    o = buy(1)["order"]
    service.start_payment(o["id"])
    with db.tx() as c:
        c.execute("UPDATE orders SET expires_at = ? WHERE id=?", (time.time() - 1, o["id"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(service.handle_payment_callback, "race-1", o["id"], "success")
        f2 = pool.submit(service.expire_stale)
        f1.result(); f2.result()
    assert service.get_order(o["id"])["state"] in ("CONFIRMED", "EXPIRED")
    assert service.check_invariants()["ok"]