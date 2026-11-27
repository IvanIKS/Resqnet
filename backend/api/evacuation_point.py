"""Routes for evacuation points."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2 import WKTElement
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import EvacuationPointIn, EvacuationPointOut
from database import get_session
from models import SRID, EvacuationPoint

router = APIRouter(tags=["evacuation-point"])


@router.post("/evacuation-point", response_model=EvacuationPointOut, status_code=201)
def create_evacuation_point(payload: EvacuationPointIn, session: Session = Depends(get_session)):
    try:
        geom = WKTElement(payload.geom, srid=SRID)
    except Exception as exc:  # malformed WKT
        raise HTTPException(status_code=400, detail=f"invalid geom WKT: {exc}")

    point = EvacuationPoint(name=payload.name, geom=geom)
    session.add(point)
    session.commit()
    session.refresh(point)
    return EvacuationPointOut(id=point.id, name=point.name)


@router.get("/evacuation-point/all", response_model=List[EvacuationPointOut])
def list_evacuation_points(session: Session = Depends(get_session)):
    points = session.scalars(select(EvacuationPoint)).all()
    return [EvacuationPointOut(id=p.id, name=p.name) for p in points]


@router.get("/evacuation-point/{point_id}", response_model=EvacuationPointOut)
def get_evacuation_point(point_id: int, session: Session = Depends(get_session)):
    point = session.get(EvacuationPoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail="evacuation point not found")
    return EvacuationPointOut(id=point.id, name=point.name)


@router.delete("/evacuation-point/{point_id}", status_code=204)
def delete_evacuation_point(point_id: int, session: Session = Depends(get_session)):
    point = session.get(EvacuationPoint, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail="evacuation point not found")
    session.delete(point)
    session.commit()
