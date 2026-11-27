import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker

from models import Base

# Default matches docker-compose; override with DATABASE_URL for local runs.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Columns added since the first schema (master). create_all() never alters a
# table that already exists, so init_db() adds these to older databases. All
# are nullable, which lets ADD COLUMN run on tables that already hold rows.
ADDED_COLUMNS = {
    "roads": ["osm_u", "osm_v", "osm_way_ids", "oneway"],
}
# Indexes added since master; create_all() skips them on existing tables.
ADDED_INDEXES = {
    "road_end1_connections": ["connected_road_id"],
    "road_end2_connections": ["connected_road_id"],
}


def init_db(bind: Engine | None = None) -> None:
    """Create missing tables and upgrade an older schema in place.

    Idempotent: safe on every start and on a database created by an older
    models.py. Runs in one transaction under an advisory lock, so two
    processes starting together do not race. The PostGIS extension itself is
    enabled by the postgis/postgis image's initdb scripts, so the Geometry
    columns resolve correctly here.
    """
    with (bind or engine).begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('resqnet.init_db'))"))
        Base.metadata.create_all(bind=conn)
        _add_missing_columns(conn)
        _add_missing_indexes(conn)


def _add_missing_columns(conn: Connection) -> None:
    inspector = inspect(conn)
    for table_name, column_names in ADDED_COLUMNS.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name in column_names:
            if name in existing:
                continue  # no ALTER, so no exclusive lock, once a column is there
            column = Base.metadata.tables[table_name].c[name]
            if not column.nullable:
                raise RuntimeError(f"{table_name}.{name} is NOT NULL and cannot be added in place")
            ddl_type = column.type.compile(dialect=conn.dialect)
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {name} {ddl_type}"))


def _add_missing_indexes(conn: Connection) -> None:
    for table_name, column_names in ADDED_INDEXES.items():
        for name in column_names:
            # Same name create_all() gives index=True, so fresh and upgraded
            # databases end up identical.
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_{name} ON {table_name} ({name})"
            ))


def get_session():
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# user roles -- dispatcher, mobile_user
