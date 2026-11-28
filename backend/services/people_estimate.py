"""How many people are inside the flood zones of a disaster.

Each settlement's population is assumed to be spread evenly over its polygon,
so a zone holds ``population * flooded share of the settlement area``. Areas
are measured on the geography (square metres), not in degrees. Every zone is
counted on its own; the total uses the union of the zones, so a village under
two overlapping zones is not counted twice.

A zone counts at moment ``at`` when it is a satellite observation that is
still valid (observed_at <= at < valid_until), or, for a zone drawn by hand
without observed_at, once its disaster has started.
"""

from datetime import datetime, timezone

from geoalchemy2 import Geography
from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.orm import Session

from models import Disaster, DisasterArea, Settlement

METHOD = "settlement_population_by_area"


def _area_m2(geom):
    return func.ST_Area(cast(geom, Geography))


def _share(zone_geom):
    return _area_m2(func.ST_Intersection(Settlement.area, zone_geom)) / func.nullif(_area_m2(Settlement.area), 0)


def _current_zones(disaster_id: int, at: datetime):
    naive = at.astimezone(timezone.utc).replace(tzinfo=None)
    return and_(
        DisasterArea.disaster_id == disaster_id,
        or_(
            and_(DisasterArea.observed_at.is_not(None),
                 DisasterArea.observed_at <= at, DisasterArea.valid_until > at),
            and_(DisasterArea.observed_at.is_(None),
                 select(Disaster.creation_time).where(Disaster.id == disaster_id).scalar_subquery() <= naive),
        ),
    )


def estimate_people(session: Session, disaster_id: int, at: datetime | None = None) -> dict:
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    current = _current_zones(disaster_id, at)

    zones = {
        zone_id: {"area_id": zone_id, "name": name, "people": 0, "settlements": []}
        for zone_id, name in session.execute(
            select(DisasterArea.id, DisasterArea.name).where(current).order_by(DisasterArea.id))
    }

    rows = session.execute(
        select(DisasterArea.id, Settlement.id, Settlement.name, Settlement.population, _share(DisasterArea.area))
        .join(Settlement, func.ST_Intersects(Settlement.area, DisasterArea.area))
        .where(current)
        .order_by(DisasterArea.id, Settlement.id)
    ).all()
    for zone_id, settlement_id, name, population, share in rows:
        share = min(float(share or 0), 1.0)
        people = round((population or 0) * share)
        zones[zone_id]["people"] += people
        zones[zone_id]["settlements"].append({
            "id": settlement_id, "name": name, "population": population,
            "share": round(share, 4), "people": people,
        })

    flooded = (
        select(Settlement.population, _share(func.ST_Union(DisasterArea.area)).label("share"))
        .join(DisasterArea, func.ST_Intersects(Settlement.area, DisasterArea.area))
        .where(current)
        .group_by(Settlement.id)
    ).subquery()
    total = sum(
        round((population or 0) * min(float(share or 0), 1.0))
        for population, share in session.execute(select(flooded.c.population, flooded.c.share))
    )

    return {
        "disaster_id": disaster_id,
        "at": at.astimezone(timezone.utc).isoformat(),
        "method": METHOD,
        "total_people": total,
        "zones": list(zones.values()),
    }
