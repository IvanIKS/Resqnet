from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_session
from models import Disaster
from services.people_estimate import estimate_people

router = APIRouter(prefix="/api/disasters", tags=["disaster"])


@router.get("/{disaster_id}/people-estimate")
def people_estimate(
    disaster_id: int,
    at: datetime | None = Query(None, description="Estimate at this moment (ISO 8601). Defaults to now."),
    session: Session = Depends(get_session),
):
    """People inside each flood zone of the disaster, and in all of them together."""
    if session.get(Disaster, disaster_id) is None:
        raise HTTPException(status_code=404, detail="disaster not found")
    return estimate_people(session, disaster_id, at)
