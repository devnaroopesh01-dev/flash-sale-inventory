# Flash-Sale Distributed Inventory

100 phones. 10,000 buyers. A few seconds.
**Rule: never sell a unit that does not exist.**

Built for the Techcora Advanced System Design Challenge (PS 4).

## What it does

A backend (FastAPI + SQLite) that sells limited stock safely under heavy load, plus a website that shows it working live.

- Reserves stock for a short time while the buyer pays
- Handles retries, duplicate payment callbacks, payment timeouts, cancellations (even after payment) and crashes
- Splits stock across 3 warehouses (50 / 30 / 20). An order of several units is all-or-nothing and can be split across warehouses.
- Website with a product page, live stock, an order flow, a stress test, performance numbers and a "protections fired" panel

## Run it

```bash
python -m venv venv
# Windows PowerShell:  venv\Scripts\Activate.ps1
# Mac / Linux:         source venv/bin/activate
pip install -r requirements.txt

pytest -q                          # 21 tests
python scripts/crash_demo.py       # kills the server twice, then recovers
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

For a true concurrency test (a browser can only open about 6 connections at once), start the server with `RESERVATION_TTL=45`, then in a second terminal:

```bash
python scripts/load_test.py        # 10,000 buyers vs 100 units
```

On a slow computer, set `USERS=3000` first (Windows: `$env:USERS="3000"`).

### Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `RESERVATION_TTL` | 120 | Seconds a reservation is held before it expires |
| `MAX_QTY` | 5 | Most units in one order |
| `REFUND_DELAY` | 2 | Seconds before the simulated payment provider finishes a refund |
| `SYNCHRONOUS` | FULL | `FULL`: every commit reaches the disk. `NORMAL`: faster, but a power cut could lose the last few commits |
| `FAULT_INJECTION` | 0 | `1` enables the Reset button and `/admin/crash` (demo only) |
| `DB_PATH` | flashsale.db | Where the SQLite file lives |

### Troubleshooting

- **"was created by an older version of this project":** stop the server, delete `flashsale.db`, `flashsale.db-wal` and `flashsale.db-shm`, then start again.
- **Crash demo says port 8765 is in use:** a server from an earlier run is still alive. Close it (Windows): `Get-NetTCPConnection -LocalPort 8765 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`

## Project structure

```
app/
  config.py          settings
  db.py              tables + the transaction helper
  service.py         ALL business logic
  main.py            API, background worker, startup recovery, response-time meter
  static/index.html  the website
tests/               race conditions, retries, expiry, refunds, split orders, metrics
scripts/
  load_test.py       10,000 buyers vs 100 units
  crash_demo.py      kill the server, restart, check stock
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
- Every move is checked against an allowed-moves table and saved in `order_events` (audit trail).
- A payment that arrives after the order is EXPIRED, CANCELLED or PAYMENT_FAILED never sells stock. It goes to the `refunds` table.
- AVAILABLE is tracked as a per-warehouse count (`warehouses.available`), not one row per unit.

## How each problem is handled

| Problem | Solution |
|---|---|
| Thousands of buyers at once | `BEGIN IMMEDIATE` takes the write lock first. Stock changes happen one at a time, so two buyers can never both see "1 left". Repeat and sold-out requests use a read-only fast path. |
| Overselling, even from a bug | `UPDATE ... WHERE available >= n` and a database `CHECK (available >= 0)` |
| Duplicate client requests | `Idempotency-Key` is UNIQUE. A retry returns the original order and takes no second unit. |
| Idempotency key reused by someone else | The key is tied to the user, product and quantity. A different request with the same key gets `key_conflict` (422) and never sees the other order. |
| One user buying many | A partial unique index allows one live order per user per product |
| Orders of several units | All or nothing. Units come from the warehouse with the most stock first, and the order is split across warehouses when one is short. The `allocations` table remembers which warehouse gave how many. |
| Duplicate payment callbacks | The payment `event_id` is saved in the SAME transaction as the state change |
| Payment timeout or abandoned cart | Reservations expire. A background worker releases the units. |
| Payment after expiry | The order stays EXPIRED, nothing is sold, and a refund row is queued |
| Cancelling a paid order | Units go back on sale at once, a refund starts (REFUND_PENDING), and the background worker finishes it (REFUNDED). Cancelling twice changes nothing. |
| Crash after reservation or payment | Stock change and order change are one transaction, all or nothing. On restart: expire old reservations, finish pending refunds, rebuild stock from the orders table, run the invariant check. |
| Power cut | SQLite runs with `synchronous=FULL`, so a committed payment is on disk |
| Stock released twice | The state change is guarded (`WHERE state IN (...)`), so release happens at most once per order |
| Multiple warehouses | Each has its own count. The one with the most stock is used first. |

The rules checked after every test and live on the website:
- For every warehouse, `available + units held by reserved/pending/confirmed orders == total`
- Every order has exactly as many units allocated as it asked for
- Confirmed units never exceed the stock

## API

| Call | What it does |
|---|---|
| `POST /orders` (header `Idempotency-Key`, body `user_id`, `quantity`) | Reserve units. 201 created, 200 replay, 409 sold out / not enough stock / already has an order, 422 key reused for a different request |
| `POST /orders/{id}/pay` | Start payment, returns a payment reference |
| `POST /payments/callback` | Payment provider webhook `{event_id, order_id, status}` |
| `POST /orders/{id}/cancel` | Cancel. A paid order is refunded and its units go back on sale. |
| `GET /orders/{id}` | Order, the warehouses that served it, and its history |
| `GET /inventory`, `GET /config` | Stock numbers and settings |
| `GET /admin/invariants`, `/admin/metrics`, `/admin/perf` | Correctness check, protection counters, response times |
| `POST /admin/reset`, `/admin/crash` | Demo only (`FAULT_INJECTION=1`) |

## Website

- **Product page** with a description, a live "N left / Hurry / Sold out" line and a units selector
- **Live stock** per warehouse, unit counts and ended-order counts
- **Be a customer:** buy, retry the same request, pay, send a duplicate callback, fail a payment, cancel (and refund after payment), pay late. A confirmed order plays a short animation.
- **Stress test:** send 100 to 10,000 buyers, with 1 or mixed units per order. About 1 in 10 double-click, and winners pay, fail, never pay, or cancel after paying.
- **Performance:** requests per second, p50 and p95 response time (measured by the server), total time
- **Protections that fired:** duplicate requests replayed, duplicate callbacks blocked, late payments sent to refund, reservations expired
- **Invariant badge:** checks the database rules above, live

## Results (measured on my laptop)

| Test | Result |
|---|---|
| Unit tests (`pytest`) | 21 pass, including 500 threads racing for 100 units: exactly 100 win |
| 10,000 buyers (`load_test.py`) | exactly 100 units confirmed, never more |
| Crash demo (`crash_demo.py`) | server killed twice: paid order kept, refund finished after the crash, unpaid units returned, no oversell |
| Website stress test, 1000 buyers | ___ requests/sec, p50 ___ ms, p95 ___ ms |

## Trade-offs and limits

- **SQLite** keeps the project install-free and gives real transactions, but it allows one writer at a time on one machine. A real system would use PostgreSQL (row locks) or Redis for the hot counter.
- **"Distributed"** here means several warehouses and several worker processes sharing one database. Multi-node needs a shared database server.
- Orders can have up to 5 units (`MAX_QTY`) and are all-or-nothing. A customer can hold one live order per product.
- `synchronous=FULL` is safer but slower than `NORMAL`. Set `SYNCHRONOUS=NORMAL` to trade safety for speed.
- **Payments are simulated.** A refund is marked paid after `REFUND_DELAY` seconds. There is no check with a payment provider when a callback never arrives.
- A late payment on an expired order is refunded, not re-sold, even if stock is still free.
- Fixed reservation time. It does not extend when payment starts.
- The two protection counters (replays, duplicate callbacks) live in server memory and reset when the server restarts. Expired and refund counts come from the database.
- The browser limits parallel requests (about 6 per server). Use `load_test.py` and `pytest` for true concurrency.
- Not built: queue or waiting room, rate limiting, login.