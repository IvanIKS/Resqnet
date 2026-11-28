"""Pydantic request/response schemas for the API layer."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from service.ingestion_flood import _require_aware
from services.road_status import RouteStatus, StatusReason


class DisasterIn(BaseModel):
    name: str
    disaster_type: int = Field(..., description="disaster_types.id")
    creation_time: datetime | None = Field(
        default=None, description="Defaults to now (UTC) when omitted."
    )


class DisasterOut(BaseModel):
    id: int
    name: str
    disaster_type: int
    creation_time: datetime


class DisasterAreaIn(BaseModel):
    name: str
    disaster_id: int = Field(..., description="disasters.id")
    area: str = Field(..., description="Polygon geometry as WKT, e.g. 'POLYGON((...))'")


class DisasterAreaOut(BaseModel):
    id: int
    name: str
    disaster_id: int


class EvacuationPointIn(BaseModel):
    name: str | None = None
    geom: str = Field(..., description="Point geometry as WKT, e.g. 'POINT(30.5 50.4)'")


class EvacuationPointOut(BaseModel):
    id: int
    name: str | None


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=6, max_length=255)
    role: str = Field(default="mobile_user", description="UserRole.role: dispatcher or mobile_user")


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class EvacuationRouteIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude (WGS84)")
    lon: float = Field(..., ge=-180, le=180, description="Longitude (WGS84)")
    at: datetime | None = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO 8601 instant with Z or a UTC offset. Defaults to now (UTC).",
    )

    @field_validator("at", mode="before")
    @classmethod
    def validate_at(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise ValueError("at must be an ISO 8601 datetime with Z or a UTC offset")
        return _require_aware(value, "at")


class EvacuationRouteOut(BaseModel):
    status: Literal["route_found", "evacuation_point_fallback", "candidate_fallback"]
    at: datetime
    route_status: RouteStatus
    reason: StatusReason | None
    limiting_segment_ids: list[int]
    start_in_flood: bool
    route: dict | None
    detail: str | None
    in_disaster_area: bool
    road_ids: list[int]
    distance_meters: float | None = None
    target_point: EvacuationPointOut | None = None
    candidate_lat: float | None = None
    candidate_lon: float | None = None


class FieldReportIn(BaseModel):
    road_id: int | None = Field(default=None, description="roads.id the report is about")
    point: str = Field(..., description="Where it was observed, WKT, e.g. 'POINT(26.05 48.25)'")
    status: Literal["passable", "blocked", "flooded"]
    observed_at: datetime | None = Field(
        default=None, description="ISO 8601 with Z or a UTC offset. Defaults to now (UTC).",
    )
    osnma_sample: Literal["ublox_authentic", "forged"] | None = Field(
        default=None,
        description="Recorded Galileo navigation data to check with OSNMA. Omit to skip the check.",
    )

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value):
        return None if value is None else _require_aware(value, "observed_at")


class FieldReportOut(BaseModel):
    id: int
    road_id: int | None
    lon: float
    lat: float
    status: str
    observed_at: datetime
    osnma_sample: str | None
    osnma_verdict: str
    osnma_detail: dict | None
    created_at: datetime
