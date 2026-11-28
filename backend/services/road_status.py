"""Road evidence at a moment, e.g. 2020-06-27T00:00:00Z.

Closed is red (flood evidence), mapped_option is grey ("Not checked", no
water seen on the image), and unknown is amber hatched ("No current data").
A field report closes a road only if its Galileo data passed the OSNMA check;
no report ever opens a road.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Literal

from sqlalchemy import and_, case, exists, func, select
from sqlalchemy.orm import Session

from models import (
    COVERAGE_KIND_ANALYSIS_EXTENT,
    COVERAGE_KIND_NOT_ANALYSED,
    FIELD_STATUS_CLOSING,
    SRID,
    AnalysisCoverage,
    DisasterArea,
    FieldReport,
    Road,
)
from service.ingestion_flood import _require_aware, valid_at

RouteStatus = Literal["closed", "mapped_option", "unknown"]
StatusReason = Literal["untimed_report", "field_report", "expired", "gap", "error"]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class SegmentStatus:
    route_status: RouteStatus
    reason: StatusReason | None = None


def road_statuses(
    session: Session,
    at: datetime,
    *,
    road_ids: Collection[int] | None = None,
    bbox: BBox | None = None,
) -> dict[int, SegmentStatus]:
    """Classify roads in one query. With no scope supplied, use every road."""
    _require_aware(at, "at")

    water = exists().where(func.ST_Intersects(DisasterArea.area, Road.geom))
    current_water = water.where(valid_at(DisasterArea, at))
    untimed_water = water.where(DisasterArea.observed_at.is_(None))

    extent = exists().where(
        AnalysisCoverage.kind == COVERAGE_KIND_ANALYSIS_EXTENT,
        func.ST_CoveredBy(Road.geom, AnalysisCoverage.area),
    )
    current_coverage = extent.where(valid_at(AnalysisCoverage, at))
    expired_coverage = extent.where(
        AnalysisCoverage.observed_at < at, AnalysisCoverage.valid_until <= at,
    )
    not_analysed = exists().where(
        AnalysisCoverage.kind == COVERAGE_KIND_NOT_ANALYSED,
        valid_at(AnalysisCoverage, at),
        func.ST_Intersects(AnalysisCoverage.area, Road.geom),
    )

    # Latest OSNMA-authenticated report about the road at ``at``; a later
    # "passable" report cancels an earlier "blocked", never satellite water.
    latest_report = (
        select(FieldReport.status)
        .where(
            FieldReport.road_id == Road.id,
            FieldReport.osnma_verdict == "authenticated",
            FieldReport.observed_at <= at,
        )
        .order_by(FieldReport.observed_at.desc(), FieldReport.id.desc())
        .limit(1)
        .correlate(Road)
        .scalar_subquery()
    )

    classification = case(
        (current_water, "closed"),
        (latest_report.in_(FIELD_STATUS_CLOSING), "field_report"),
        (untimed_water, "untimed_report"),
        (and_(current_coverage, ~not_analysed), "mapped_option"),
        # A current Not Analysed mask is the current evidence; an older extent must not relabel it.
        (current_coverage, "gap"),
        (expired_coverage, "expired"),
        else_="gap",
    )
    stmt = select(Road.id, classification)
    if road_ids is not None:
        stmt = stmt.where(Road.id.in_(road_ids))
    if bbox is not None:
        stmt = stmt.where(Road.geom.op("&&")(func.ST_MakeEnvelope(*bbox, SRID)))

    statuses = {
        "closed": SegmentStatus("closed"),
        "untimed_report": SegmentStatus("closed", "untimed_report"),
        "field_report": SegmentStatus("closed", "field_report"),
        "mapped_option": SegmentStatus("mapped_option"),
        "expired": SegmentStatus("unknown", "expired"),
        "gap": SegmentStatus("unknown", "gap"),
    }
    return {road_id: statuses[key] for road_id, key in session.execute(stmt)}
