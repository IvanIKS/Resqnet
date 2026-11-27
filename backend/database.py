import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

# Default matches docker-compose; override with DATABASE_URL for local runs.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://resqnet:resqnet@localhost:5432/resqnet",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create all tables defined on the models' metadata.

    The PostGIS extension itself is enabled by the postgis/postgis image's
    initdb scripts, so the Geometry columns resolve correctly here.
    """
    Base.metadata.create_all(bind=engine)


def get_session():
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# user roles -- dispatcher, mobile_user
