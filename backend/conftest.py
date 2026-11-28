"""Shared pytest fixtures.

The route handlers only touch a small, well-defined slice of the SQLAlchemy
Session API, so the tests run against an in-memory fake instead of a live
PostGIS database. This keeps CRUD tests fast and infrastructure-free while
still exercising the real routes, schemas and service wiring.
"""

import pytest
from fastapi.testclient import TestClient

from database import get_session
from main import app
from models import DisasterType
from services.registry import get_alarm_service


class FakeSession:
    """Minimal stand-in for a SQLAlchemy Session backed by dicts.

    Objects are stored per model class keyed by a monotonically increasing id,
    mimicking autoincrement primary keys assigned at commit time.
    """

    def __init__(self, store: dict, counters: dict):
        self.store = store
        self.counters = counters
        self._new: list = []
        self._deleted: list = []

    def get(self, model, ident):
        return self.store.get(model, {}).get(ident)

    def add(self, obj):
        self._new.append(obj)

    def delete(self, obj):
        self._deleted.append(obj)

    def commit(self):
        for obj in self._new:
            model = type(obj)
            if getattr(obj, "id", None) is None:
                self.counters[model] = self.counters.get(model, 0) + 1
                obj.id = self.counters[model]
            self.store.setdefault(model, {})[obj.id] = obj
        for obj in self._deleted:
            self.store.get(type(obj), {}).pop(getattr(obj, "id", None), None)
        self._new.clear()
        self._deleted.clear()

    def refresh(self, obj):  # id is already assigned in commit()
        pass

    def scalars(self, stmt):
        model = stmt.column_descriptions[0]["entity"]
        return _ScalarResult(list(self.store.get(model, {}).values()))

    def close(self):
        pass


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class SpyAlarmService:
    """Records the disasters it was asked to raise an alarm for."""

    def __init__(self):
        self.raised: list[int] = []

    def raise_alarm(self, disaster):
        self.raised.append(disaster.id)


@pytest.fixture
def store():
    """Fresh data store seeded with one disaster type (id=1)."""
    data: dict = {}
    counters: dict = {}
    seeded = DisasterType(name="flood")
    seeded.id = 1
    data[DisasterType] = {1: seeded}
    counters[DisasterType] = 1
    return {"data": data, "counters": counters}


@pytest.fixture
def alarm_spy():
    return SpyAlarmService()


@pytest.fixture
def client(store, alarm_spy):
    def override_get_session():
        yield FakeSession(store["data"], store["counters"])

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_alarm_service] = lambda: alarm_spy
    # Instantiated without a `with` block so the lifespan (init_db / real
    # Postgres connection) is not triggered.
    yield TestClient(app)
    app.dependency_overrides.clear()
