"""Tests run against a throwaway PostGIS database next to the dev one.

Start the stack first (docker compose up -d db), then: pytest
"""

import os
"""Make backend/ importable (``models``, ``service.*``) however pytest is started."""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ADMIN_URL = os.getenv("TEST_ADMIN_URL", "postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet")
TEST_DB = "resqnet_test"

admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
with admin.connect() as conn:
    if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}):
        conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
admin.dispose()

os.environ["DATABASE_URL"] = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import engine  # noqa: E402
from models import Base  # noqa: E402

with engine.connect() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    conn.commit()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from main import app

    Base.metadata.drop_all(bind=engine)
    with TestClient(app) as c:
        yield c

BACKEND = str(Path(__file__).resolve().parents[1])
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
