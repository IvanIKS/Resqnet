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
