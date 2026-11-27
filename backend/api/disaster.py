"""Routes for disasters and their affected areas."""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2 import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import DisasterAreaIn, DisasterAreaOut, DisasterIn, DisasterOut
from database import get_session
from models import SRID, Disaster, DisasterArea, DisasterType
from services import alarm

router = APIRouter(tags=["disaster"])


@router.post("/disaster", response_model=DisasterOut, status_code=201)
def create_disaster(payload: DisasterIn, session: Session = Depends(get_session)):
    if session.get(DisasterType, payload.disaster_type) is None:
        raise HTTPException(status_code=404, detail="disaster_type not found")

    disaster = Disaster(
        name=payload.name,
        disaster_type=payload.disaster_type,
        creation_time=payload.creation_time or datetime.utcnow(),
    )
    session.add(disaster)
    session.commit()
    session.refresh(disaster)

    # Declaring a disaster automatically raises an alarm.
    alarm.raise_alarm(disaster)

    return DisasterOut(
        id=disaster.id,
        name=disaster.name,
        disaster_type=disaster.disaster_type,
        creation_time=disaster.creation_time,
    )


@router.get("/disaster/all", response_model=List[DisasterOut])
def list_disasters(session: Session = Depends(get_session)):
    disasters = session.scalars(select(Disaster)).all()
    return [
        DisasterOut(
            id=d.id,
            name=d.name,
            disaster_type=d.disaster_type,
            creation_time=d.creation_time,
        )
        for d in disasters
    ]


@router.get("/disaster/{disaster_id}", response_model=DisasterOut)
def get_disaster(disaster_id: int, session: Session = Depends(get_session)):
    disaster = session.get(Disaster, disaster_id)
    if disaster is None:
        raise HTTPException(status_code=404, detail="disaster not found")
    return DisasterOut(
        id=disaster.id,
        name=disaster.name,
        disaster_type=disaster.disaster_type,
        creation_time=disaster.creation_time,
    )


@router.delete("/disaster/{disaster_id}", status_code=204)
def delete_disaster(disaster_id: int, session: Session = Depends(get_session)):
    disaster = session.get(Disaster, disaster_id)
    if disaster is None:
        raise HTTPException(status_code=404, detail="disaster not found")
    session.delete(disaster)
    session.commit()


@router.post("/disasterArea", response_model=DisasterAreaOut, status_code=201)
def create_disaster_area(payload: DisasterAreaIn, session: Session = Depends(get_session)):
    if session.get(Disaster, payload.disaster_id) is None:
        raise HTTPException(status_code=404, detail="disaster not found")

    try:
        area = WKTElement(payload.area, srid=SRID)
    except Exception as exc:  # malformed WKT
        raise HTTPException(status_code=400, detail=f"invalid area WKT: {exc}")

    disaster_area = DisasterArea(
        name=payload.name,
        disaster_id=payload.disaster_id,
        area=area,
    )
    session.add(disaster_area)
    session.commit()
    session.refresh(disaster_area)
    return DisasterAreaOut(
        id=disaster_area.id,
        name=disaster_area.name,
        disaster_id=disaster_area.disaster_id,
    )


@router.get("/disasterArea/all", response_model=List[DisasterAreaOut])
def list_disaster_areas(session: Session = Depends(get_session)):
    areas = session.scalars(select(DisasterArea)).all()
    return [
        DisasterAreaOut(id=a.id, name=a.name, disaster_id=a.disaster_id)
        for a in areas
    ]


@router.get("/disasterArea/{area_id}", response_model=DisasterAreaOut)
def get_disaster_area(area_id: int, session: Session = Depends(get_session)):
    area = session.get(DisasterArea, area_id)
    if area is None:
        raise HTTPException(status_code=404, detail="disaster area not found")
    return DisasterAreaOut(id=area.id, name=area.name, disaster_id=area.disaster_id)


@router.delete("/disasterArea/{area_id}", status_code=204)
def delete_disaster_area(area_id: int, session: Session = Depends(get_session)):
    area = session.get(DisasterArea, area_id)
    if area is None:
        raise HTTPException(status_code=404, detail="disaster area not found")
    session.delete(area)
    session.commit()
