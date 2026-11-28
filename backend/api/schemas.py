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
