import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from generate_synthetic_data import generate, write  # noqa: E402

from app.db import connect_writable  # noqa: E402
from app.ingest.statements import load, parse_statement  # noqa: E402


@pytest.fixture(scope="session")
def statement(tmp_path_factory) -> tuple[Path, Path]:
    out = tmp_path_factory.mktemp("statement") / "transactions.csv"
    truth = write(generate(date(2025, 11, 1), 12, seed=42), out)
    return out, truth


@pytest.fixture(scope="session")
def db_path(statement, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "test.db"
    conn = connect_writable(path)
    load(conn, parse_statement(statement[0]))
    conn.close()
    return path
