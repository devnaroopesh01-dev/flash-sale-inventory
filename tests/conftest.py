import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Use a throwaway database for tests
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _tmp.name
os.environ["RESERVATION_TTL"] = "60"

from app import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db()
    db.reset_all()
    yield