"""Field reports about road conditions, checked with Galileo OSNMA."""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from geoalchemy2 import WKTElement
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from api.schemas import FieldReportIn, FieldReportOut
from database import get_session
from models import SRID, FieldReport, Road
from services import osnma

router = APIRouter(prefix="/api/reports", tags=["field-report"])
logger = logging.getLogger("resqnet.field_report")


def _check_point(session: Session, wkt: str) -> None:
    try:
        kind = session.scalar(select(func.ST_GeometryType(func.ST_GeomFromText(wkt, SRID))))
    except DBAPIError:
        session.rollback()
        kind = None
    if kind != "ST_Point":
        raise HTTPException(status_code=400, detail="point must be a WKT POINT, e.g. 'POINT(26.05 48.25)'")


def _report_query():
    return select(FieldReport, func.ST_X(FieldReport.point), func.ST_Y(FieldReport.point))


def _out(report: FieldReport, lon: float, lat: float) -> FieldReportOut:
    return FieldReportOut(
        id=report.id, road_id=report.road_id, lon=lon, lat=lat, status=report.status,
        observed_at=report.observed_at, osnma_sample=report.osnma_sample,
        osnma_verdict=report.osnma_verdict, osnma_detail=report.osnma_detail,
        created_at=report.created_at,
    )


@router.post("", response_model=FieldReportOut, status_code=201)
def create_report(payload: FieldReportIn, session: Session = Depends(get_session)):
    if payload.road_id is not None and session.get(Road, payload.road_id) is None:
        raise HTTPException(status_code=404, detail="road not found")
    _check_point(session, payload.point)

    now = datetime.now(timezone.utc)
    verdict, detail = "not_checked", None
    if payload.osnma_sample:
        try:
            result = osnma.check_sample(payload.osnma_sample)
            verdict, detail = result["verdict"], result["detail"]
        except Exception:
            logger.exception("OSNMA check failed, report kept as not_checked")
            detail = {"sample": payload.osnma_sample, "error": "OSNMA check unavailable"}

    report = FieldReport(
        road_id=payload.road_id,
        point=WKTElement(payload.point, srid=SRID),
        status=payload.status,
        observed_at=(payload.observed_at or now).astimezone(timezone.utc),
        osnma_sample=payload.osnma_sample,
        osnma_verdict=verdict,
        osnma_detail=detail,
        created_at=now,
    )
    session.add(report)
    session.commit()
    row = session.execute(_report_query().where(FieldReport.id == report.id)).one()
    return _out(*row)


@router.get("", response_model=List[FieldReportOut])
def list_reports(session: Session = Depends(get_session)):
    rows = session.execute(_report_query().order_by(FieldReport.id)).all()
    return [_out(*row) for row in rows]
