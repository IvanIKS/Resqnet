"""Pydantic request/response schemas for the API layer."""

from datetime import datetime

from pydantic import BaseModel, Field


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


class EvacuationRouteIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude (WGS84)")
    lon: float = Field(..., ge=-180, le=180, description="Longitude (WGS84)")


class EvacuationRouteOut(BaseModel):
    status: str
    in_disaster_area: bool
    road_ids: list[int]
    distance_meters: float | None = None
    target_point: EvacuationPointOut | None = None
    candidate_lat: float | None = None
    candidate_lon: float | None = None
