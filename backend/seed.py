"""Idempotent database seeder.

Inserts the baseline reference data the app expects to exist:
  * user roles: ``dispatcher`` and ``mobile_user``
  * disaster type: ``disaster``

Run standalone (``python seed.py``) after the tables are created, or import
``seed`` and call it with an existing session.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from models import UserRole, DisasterType

USER_ROLES = ["dispatcher", "mobile_user"]
DISASTER_TYPES = ["disaster"]


def _ensure_role(session: Session, role: str) -> None:
    exists = session.scalar(select(UserRole).where(UserRole.role == role))
    if exists is None:
        session.add(UserRole(role=role))


def _ensure_disaster_type(session: Session, name: str) -> None:
    exists = session.scalar(select(DisasterType).where(DisasterType.name == name))
    if exists is None:
        session.add(DisasterType(name=name))


def seed(session: Session) -> None:
    """Insert baseline rows if they are missing. Safe to run repeatedly."""
    for role in USER_ROLES:
        _ensure_role(session, role)
    for name in DISASTER_TYPES:
        _ensure_disaster_type(session, name)
    session.commit()


def main() -> None:
    init_db()
    with SessionLocal() as session:
        seed(session)
    print("Seed complete: roles and disaster types ensured.")


if __name__ == "__main__":
    main()
