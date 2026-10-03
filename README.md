# Flash-Sale Distributed Inventory

100 phones. 10,000 buyers. A few seconds.
**Rule: never sell a unit that does not exist.**

Built for the Techcora Advanced System Design Challenge (PS 4).

## What it does

A backend (FastAPI + SQLite) that sells limited stock safely under heavy load, plus a website that shows it working live.

- Reserves stock for a short time while the buyer pays
- Handles retries, duplicate payment callbacks, payment timeouts, cancellations and crashes
- Splits stock across 3 warehouses (50 / 30 / 20)
- Website with live stock, an order flow, a stress test, performance numbers and a "protections fired" panel

## Run it

```bash
python -m venv venv
# Windows PowerShell:  venv\Scripts\Activate.ps1
# Mac / Linux:         source venv/bin/activate
pip install -r requirements.txt

pytest -q                          # 11 tests
python scripts/crash_demo.py       # kills the server, then recovers
```

Start the website and API:

```bash
# Windows PowerShell
$env:FAULT_INJECTION="1"; $env:RESERVATION_TTL="30"
python -m uvicorn app.main:app --port 8000

# Mac / Linux
FAULT_INJECTION=1 RESERVATION_TTL=30 uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000

`FAULT_INJECTION=1` enables the Reset button. `RESERVATION_TTL` is how many seconds a reservation is held.

For a true concurrency test (the browser can only open about 6 connections at once), run the server with `RESERVATION_TTL=45`, then in a second terminal:

```bash
python scripts/load_test.py        # 10,000 buyers vs 100 units
```

## Project structure

```
app/
  config.py        settings (stock, reservation time)
  db.py            tables + the transaction helper
  service.py       ALL business logic
  main.py          API, expiry worker, startup recovery, response-time meter
  static/index.html  the website
tests/             race conditions, retries, expiry, refunds, metrics
scripts/
  load_test.py     10,000 buyers vs 100 units
  crash_demo.py    kill the server, restart, check stock
```

## State machine

```
AVAILABLE --reserve--> RESERVED --pay--> PAYMENT_PENDING --payment ok--> CONFIRMED
                          |                    |                             |
                          |                    |                  cancel --> REFUND_PENDING --> REFUNDED
                          +--------------------+--> EXPIRED        (reservation time ran out)
                          +--------------------+--> CANCELLED      (customer cancelled)
                          +--------------------+--> PAYMENT_FAILED (payment failed)
```

- EXPIRED, CANCELLED, PAYMENT_FAILED and REFUND_PENDING put the units back to AVAILABLE.
- Every move is checked against an allowed-moves table, and saved in `order_events` (audit trail).
- A payment that arrives after the order is EXPIRED, CANCELLED or PAYMENT_FAILED never sells stock. It goes to the `refunds` table.
- AVAILABLE is tracked as a per-warehouse count (`warehouses.available`), not one row per unit.

## How each problem is handled

| Problem | Solution |
|---|---|
| Thousands of buyers at once | `BEGIN IMMEDIATE` takes the write lock first. Stock changes happen one at a time, so two buyers can never both see "1 left". Repeat and sold-out requests use a read-only fast path. |
| Overselling, even from a bug | `UPDATE ... WHERE available > 0` and a database `CHECK (available >= 0)` |
| Duplicate client requests | `Idempotency-Key` is UNIQUE. A retry returns the original order and takes no second unit. |
| One user buying many | A partial unique index allows one live order per user per product |
| Duplicate payment callbacks | The payment `event_id` is saved in the SAME transaction as the state change |
| Payment timeout or abandoned cart | Reservations expire. A background worker releases the unit. |
| Payment after expiry | Order stays EXPIRED, and a refund row is queued |
| Crash after reservation or payment | Stock change and order change are one transaction, all or nothing. On restart: expire old reservations, rebuild stock from the orders table, run the invariant check. |
| Stock released twice | State change is guarded (`WHERE state IN (...)`), so release happens at most once per order |
| Multiple warehouses | Each has its own count. The one with the most stock is used first. |

The rule checked after every test:
`available + (reserved + pending + confirmed) == total` for every warehouse, and confirmed never exceeds stock.

## Website

- **Live stock** per warehouse and the count of orders in each state
- **Be a customer:** buy, retry the same request, pay, send a duplicate callback, cancel, pay late
- **Stress test:** send 100 to 10,000 buyers. About 1 in 10 double-click, and winners pay, fail or never pay.
- **Performance:** requests per second, p50 and p95 response time (measured by the server), total time
- **Protections that fired:** duplicate requests replayed, duplicate callbacks blocked, late payments sent to refund, reservations expired
- **Invariant badge:** checks the database rule above, live

## Results (measured on my laptop)

| Test | Result |
|---|---|
| 500 threads racing for 100 units | exactly 100 win |
| 10,000 buyers (`load_test.py`) | exactly 100 confirmed, never more |
| Crash demo (`kill` then restart) | confirmed orders kept, unpaid units returned, no oversell |
| Website stress test, ___ buyers | ___ requests/sec, p50 ___ ms, p95 ___ ms |

## Trade-offs and limits

- **SQLite** keeps the project install-free and gives real transactions, but it allows one writer at a time on one machine. A real system would use PostgreSQL (row locks) or Redis for the hot counter.
- **"Distributed"** here means several warehouses and several worker processes sharing one database. Multi-node needs a shared database server.
- **Every order is 1 unit.** Splitting a multi-unit order across warehouses is not built.
- **Durability:** `synchronous=NORMAL` is safe against a process crash. A power cut could lose the last few commits. `FULL` would fix this at some cost in speed.
- **Payments are simulated.** Refunds are recorded, not paid. There is no check with a payment provider when a callback never arrives.
- A confirmed order cannot be cancelled with refund and restock.
- Fixed reservation time. It does not extend when payment starts.
- The two protection counters (replays, duplicate callbacks) live in server memory and reset when the server restarts. Expired and refund counts come from the database.
- The browser limits parallel requests (about 6 per server). Use `load_test.py` and `pytest` for true concurrency.
- Not built: queue or waiting room, rate limiting, login.