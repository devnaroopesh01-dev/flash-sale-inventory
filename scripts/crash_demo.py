"""Crash recovery demo. It starts and kills the server by itself:

    python scripts/crash_demo.py

Story
  1. Make 6 orders (the last one asks for 2 units). Confirm 2 of them.
  2. Kill the server instantly (like a power cut).
  3. Restart on the same database. Stock must still be correct.
  4. The payment provider re-sends an old callback. It must be ignored.
  5. Cancel a PAID order (refund starts), then kill the server again before the refund finishes.
  6. Restart. The refund finishes by itself, unpaid reservations expire, nothing is lost or oversold.
"""
import os
import subprocess
import sys
import tempfile
import time

import httpx

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(tempfile.mkdtemp(), "crash_demo.db")


def start():
    env = dict(os.environ, DB_PATH=DB, RESERVATION_TTL="4", EXPIRY_INTERVAL="1", REFUND_DELAY="3", FAULT_INJECTION="1")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    for _ in range(50):
        try:
            httpx.get(f"{BASE}/config", timeout=1)
            return p
        except Exception:
            time.sleep(0.2)
    p.kill()
    sys.exit("server did not start")


def crash(p):
    try:
        httpx.post(f"{BASE}/admin/crash", timeout=2)
    except Exception:
        pass
    p.wait()
    print("   server killed, exit code:", p.returncode)


def show(title):
    inv = httpx.get(f"{BASE}/inventory").json()
    chk = httpx.get(f"{BASE}/admin/invariants").json()
    print(f"  {title}: units={inv['units_by_state']} orders={inv['orders_by_state']} invariants_ok={chk['ok']}")


def state(order_id):
    return httpx.get(f"{BASE}/orders/{order_id}").json()["order"]["state"]


def main():
    print("1) Start server, make 6 orders, confirm 2")
    p = start()
    orders = []
    for i in range(6):
        qty = 2 if i == 5 else 1
        r = httpx.post(f"{BASE}/orders", json={"user_id": f"u{i}", "quantity": qty}, headers={"Idempotency-Key": f"k{i}"})
        orders.append(r.json()["order"]["id"])
    for oid in orders[:2]:
        httpx.post(f"{BASE}/orders/{oid}/pay")
        r = httpx.post(f"{BASE}/payments/callback", json={"event_id": f"evt-{oid}", "order_id": oid, "status": "success"})
        print("   callback ->", r.json()["result"])
    show("before crash")

    print("2) CRASH the server (no cleanup)")
    crash(p)

    print("3) Restart on the same database")
    p = start()
    show("after restart")

    print("4) Payment provider re-sends an old callback")
    oid = orders[0]
    r = httpx.post(f"{BASE}/payments/callback", json={"event_id": f"evt-{oid}", "order_id": oid, "status": "success"})
    print("   duplicate callback ->", r.json()["result"])

    print("5) Cancel a paid order (refund starts), then CRASH before the refund finishes")
    r = httpx.post(f"{BASE}/orders/{oid}/cancel")
    print("   cancel ->", r.json()["result"], "refund:", r.json().get("refund"), "| order state:", state(oid))
    show("units are back on sale already")
    crash(p)

    print("6) Restart. The refund must finish by itself")
    p = start()
    print("   order state right after restart:", state(oid))
    time.sleep(7)
    print("   order state a few seconds later:", state(oid))
    show("after recovery")

    chk = httpx.get(f"{BASE}/admin/invariants").json()
    inv = httpx.get(f"{BASE}/inventory").json()
    by = inv["orders_by_state"]
    assert chk["ok"], chk
    assert by.get("CONFIRMED") == 1 and by.get("REFUNDED") == 1 and by.get("EXPIRED") == 4, by
    assert inv["available_units"] == 99, inv
    print("\nPASS: the paid order survived, the cancelled paid order was refunded after the crash,")
    print("      unpaid units (including the 2-unit order) returned, and nothing was oversold.")
    p.terminate()


if __name__ == "__main__":
    main()