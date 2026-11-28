"""Lazy PostGIS fixtures for integration tests using throwaway databases."""

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import database
from models import Base


@pytest.fixture(scope="session")
def scratch_database():
    server = make_url(os.getenv("TEST_ADMIN_URL", database.DATABASE_URL))

    @contextmanager
    def open_database():
        admin = create_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT",
                              connect_args={"connect_timeout": 3})
        name = f"resqnet_test_{uuid.uuid4().hex[:10]}"
        try:
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        except OperationalError:
            admin.dispose()
            pytest.skip(f"PostGIS server unavailable at {server.host}:{server.port}")

        engine = create_engine(server.set(database=name))
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            yield engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()

    return open_database


@pytest.fixture(scope="module")
def api_engine(scratch_database):
    with scratch_database() as engine:
        yield engine


@pytest.fixture
def postgis_client(api_engine, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    Base.metadata.drop_all(bind=api_engine)
    monkeypatch.setitem(database.SessionLocal.kw, "bind", api_engine)
    monkeypatch.setattr(main, "init_db", lambda: database.init_db(api_engine))
    with TestClient(main.app) as client:
        yield client


@pytest.fixture(autouse=True)
def _fresh_road_graph_cache():
    """Scratch databases restart ids, so a cached graph could match a different test's roads."""
    from services.evacuation import clear_road_graph_cache

    clear_road_graph_cache()
    yield
    clear_road_graph_cache()
