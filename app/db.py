"""Database setup. SQLite is used so the project runs with zero installs.

The important idea: every change to stock and orders happens inside ONE
transaction, so the database can never end up half-updated after a crash.
"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_local = threading.local()

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses (
    id        TEXT PRIMARY KEY,
    sku       TEXT NOT NULL,
    total     INTEGER NOT NULL CHECK (total >= 0),
    -- Safety net: the database itself refuses to go below 0 or above total.
    available INTEGER NOT NULL CHECK (available >= 0 AND available <= total)
);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    sku             TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity >= 1),
    state           TEXT NOT NULL CHECK (state IN
        ('RESERVED','PAYMENT_PENDING','CONFIRMED',
         'EXPIRED','CANCELLED','PAYMENT_FAILED','REFUND_PENDING','REFUNDED')),
    idempotency_key TEXT NOT NULL UNIQUE,
    payment_ref     TEXT,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

-- Which warehouse(s) the units of an order come from (an order can be split)
CREATE TABLE IF NOT EXISTS allocations (
    order_id     TEXT NOT NULL REFERENCES orders(id),
    warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
    qty          INTEGER NOT NULL CHECK (qty >= 1),
    PRIMARY KEY (order_id, warehouse_id)
);
CREATE INDEX IF NOT EXISTS idx_alloc_wh ON allocations(warehouse_id);

-- A user can hold only one live order per product (enforced by the database).
CREATE UNIQUE INDEX IF NOT EXISTS one_live_order_per_user
    ON orders(user_id, sku)
    WHERE state IN ('RESERVED','PAYMENT_PENDING','CONFIRMED');

CREATE INDEX IF NOT EXISTS idx_orders_state_expiry ON orders(state, expires_at);

-- Audit trail of every state change
CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    ts         REAL NOT NULL
);

-- Payment callbacks we have already handled (stops double processing)
CREATE TABLE IF NOT EXISTS processed_events (
    event_id    TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL,
    received_at REAL NOT NULL
);

-- Money that must go back to a customer: late payments and cancelled paid orders
CREATE TABLE IF NOT EXISTS refunds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','REFUNDED')),
    created_at   REAL NOT NULL,
    processed_at REAL
);
"""


def get_conn() -> sqlite3.Connection:
    """One connection per thread, reused for speed."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            config.DB_PATH,
            timeout=30,
            isolation_level=None,  # we control transactions manually
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # FULL (default): every commit is flushed to disk, so a committed payment survives even a power cut.
        conn.execute(f"PRAGMA synchronous={config.SYNCHRONOUS}")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """Run a block as one atomic transaction.

    BEGIN IMMEDIATE takes the write lock up front, so two requests can never
    both read 'stock = 1' and both sell it. They line up one after another.
    If anything fails, ROLLBACK undoes every change in the block.
    """
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    conn = get_conn()
    has_orders = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if has_orders and version != SCHEMA_VERSION:
        raise RuntimeError(
            f"{config.DB_PATH} was created by an older version of this project. "
            "Stop the server, delete flashsale.db, flashsale.db-wal and flashsale.db-shm, then start again."
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def seed_if_empty() -> None:
    with tx() as c:
        n = c.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
        if n == 0:
            for wid, qty in config.WAREHOUSES:
                c.execute(
                    "INSERT INTO warehouses(id, sku, total, available) VALUES (?,?,?,?)",
                    (wid, config.SKU, qty, qty),
                )


def reset_all() -> None:
    """Wipe everything and re-seed. Demo/test use only."""
    with tx() as c:
        for t in ("refunds", "processed_events", "order_events", "allocations", "orders", "warehouses"):
            c.execute(f"DELETE FROM {t}")
    seed_if_empty()