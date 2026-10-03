import os

# Where the SQLite database file lives
DB_PATH = os.getenv("DB_PATH", "flashsale.db")

# How long a reservation is held before it expires (seconds)
RESERVATION_TTL = float(os.getenv("RESERVATION_TTL", "120"))

# How often the background worker runs (expires reservations, finishes refunds)
EXPIRY_INTERVAL = float(os.getenv("EXPIRY_INTERVAL", "1"))

# How long a refund stays "pending" before the (simulated) payment provider finishes it
REFUND_DELAY = float(os.getenv("REFUND_DELAY", "2"))

# Most units one order may contain
MAX_QTY = int(os.getenv("MAX_QTY", "5"))

# SQLite flush mode. FULL = every commit reaches the disk (safe even if the power is cut).
# NORMAL = faster, safe if the program crashes, but a power cut can lose the last few commits.
SYNCHRONOUS = os.getenv("SYNCHRONOUS", "FULL").upper()
if SYNCHRONOUS not in ("FULL", "NORMAL"):
    raise ValueError("SYNCHRONOUS must be FULL or NORMAL")

# Enables /admin/crash and /admin/reset (only for demos and tests)
FAULT_INJECTION = os.getenv("FAULT_INJECTION", "0") == "1"

# The product on sale and the stock in each warehouse (total = 100)
SKU = "PHONE-X"
WAREHOUSES = [("WH-1", 50), ("WH-2", 30), ("WH-3", 20)]