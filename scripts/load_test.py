"""Load test: 10,000 buyers, 100 phones. One buyer in five asks for 2 phones.

Start the server first (in another terminal):
    FAULT_INJECTION=1 RESERVATION_TTL=45 uvicorn app.main:app --port 8000

Then run:
    python scripts/load_test.py

What it does
  Wave 1: 10,000 users click "buy" at once. ~10% also send a duplicate retry.
          The winners pay: most succeed (some callbacks are sent twice),
          some fail, some never pay.
  Wait:   unpaid reservations expire and their units go back on sale.
  Wave 2: users who found no stock try again and grab the released units.
  Check:  confirmed units must never be more than the stock (100).
"""
import asyncio
import collections
import os
import random
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
USERS = int(os.getenv("USERS", "10000"))   # set USERS=3000 on a slow computer
CONCURRENCY = 100


def qty_for(user):
    """Every fifth user wants 2 phones, everyone else wants 1."""
    return 2 if int(user.replace("user", "")) % 5 == 0 else 1


async def buy(client, sem, user, key=None):
    async with sem:
        r = await client.post(
            "/orders",
            json={"user_id": user, "quantity": qty_for(user)},
            headers={"Idempotency-Key": key or f"buy-{user}"},
        )
        return r.json()


async def pay_and_callback(client, sem, order_id, outcome):
    async with sem:
        await client.post(f"/orders/{order_id}/pay")
    if outcome == "no_pay":
        return
    status = "success" if outcome in ("ok", "ok_twice") else "failed"
    event = f"evt-{order_id}"
    calls = 2 if outcome == "ok_twice" else 1
    # duplicates are sent at the same moment on purpose
    await asyncio.gather(
        *[
            client.post("/payments/callback", json={"event_id": event, "order_id": order_id, "status": status})
            for _ in range(calls)
        ]
    )


async def main():
    random.seed(1)
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(base_url=BASE, timeout=120, limits=limits) as client:
        sem = asyncio.Semaphore(CONCURRENCY)
        ttl = (await client.get("/config")).json()["reservation_ttl"]
        r = await client.post("/admin/reset")
        if r.status_code != 200:
            sys.exit("Start the server with FAULT_INJECTION=1 so /admin/reset works")

        users = [f"user{i}" for i in range(USERS)]

        # ---------------- Wave 1
        t0 = time.time()
        tasks = [buy(client, sem, u) for u in users]
        retries = [buy(client, sem, u) for u in random.sample(users, USERS // 10)]  # duplicate retries
        results = await asyncio.gather(*(tasks + retries))
        count = collections.Counter(x["result"] for x in results)
        print(f"Wave 1: {USERS + USERS // 10} requests in {time.time() - t0:.1f}s")
        print("Wave 1 results:", dict(count))

        winners = {}
        for u, res in zip(users, results[:USERS]):
            if res["result"] == "created":
                winners[u] = res["order"]["id"]
        print(f"Wave 1 winners: {len(winners)}")

        outcomes = random.choices(["ok", "ok_twice", "failed", "no_pay"], weights=[50, 25, 10, 15], k=len(winners))
        await asyncio.gather(*[
            pay_and_callback(client, sem, oid, out) for oid, out in zip(winners.values(), outcomes)
        ])
        inv = (await client.get("/inventory")).json()
        print("After payments: units", inv["units_by_state"], "orders", inv["orders_by_state"])

        # ---------------- Wait for unpaid reservations to expire
        print(f"Waiting {ttl + 3:.0f}s for unpaid reservations to expire...")
        await asyncio.sleep(ttl + 3)
        inv = (await client.get("/inventory")).json()
        print("After expiry:   units", inv["units_by_state"], "orders", inv["orders_by_state"])

        # ---------------- Wave 2: people who found no stock try again
        losers = [u for u in users if u not in winners][:500]
        res2 = await asyncio.gather(*[buy(client, sem, u, key=f"retry-{u}") for u in losers])
        count2 = collections.Counter(x["result"] for x in res2)
        print("Wave 2 results:", dict(count2))
        for x in res2:
            if x["result"] == "created":
                await pay_and_callback(client, sem, x["order"]["id"], "ok")

        # ---------------- Final check
        inv = (await client.get("/inventory")).json()
        chk = (await client.get("/admin/invariants")).json()
        print("\nFINAL: units", inv["units_by_state"], "| orders", inv["orders_by_state"])
        print("Invariant check:", chk)
        assert chk["ok"], chk
        assert chk["confirmed"] <= inv["total_units"], "OVERSOLD!"
        print(f"\nPASS: {chk['confirmed']} units confirmed, {inv['total_units']} units in stock. Never oversold.")


if __name__ == "__main__":
    asyncio.run(main())

# NOTE: wave 1 must finish before RESERVATION_TTL, otherwise units expire and are
# resold while the wave is still running (still correct, but "created" will be
# more than the stock). If your machine is slow, start the server with a bigger TTL.