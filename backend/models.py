from typing import List

from geoalchemy2 import Geometry
from sqlalchemy import Column, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase
from sqlalchemy.sql.schema import ForeignKey
from sqlalchemy.sql.sqltypes import Integer, String, DateTime

# SRID 4326 == WGS84 (lon/lat), the standard for GPS / web-map data.
SRID = 4326


class Base(DeclarativeBase):
    pass


class UserRole(Base):
    __tablename__ = 'user_roles'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(255))


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True)
    password: Mapped[str] = mapped_column(String(255))
    role: Mapped[int] = mapped_column(Integer, ForeignKey('user_roles.id'))


class DisasterType(Base):
    __tablename__ = 'disaster_types'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))


class Disaster(Base):
    __tablename__ = 'disasters'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    creation_time: Mapped[str] = mapped_column(DateTime)
    disaster_type: Mapped[int] = mapped_column(Integer, ForeignKey('disaster_types.id'))


class DisasterArea(Base):
    __tablename__ = 'disaster_areas'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    disaster_id: Mapped[int] = mapped_column(Integer, ForeignKey('disasters.id'))
    area: Mapped[object] = mapped_column(Geometry(geometry_type='POLYGON', srid=SRID))


class Settlement(Base):
    __tablename__ = 'settlements'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    population: Mapped[int] = mapped_column(Integer)
    area: Mapped[object] = mapped_column(Geometry(geometry_type='POLYGON', srid=SRID))


class EvacuationPoint(Base):
    __tablename__ = 'evacuation_points'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    geom: Mapped[object] = mapped_column(Geometry(geometry_type='POINT', srid=SRID))


# A road is a segment between two ends: end1 and end2. Each end is
# either a dead end or a crossroad where this road meets other roads. We model
# it with two many-to-many links, one association table per end
road_end1_connections = Table(
    'road_end1_connections',
    Base.metadata,
    Column('road_id', Integer, ForeignKey('roads.id'), primary_key=True),
    Column('connected_road_id', Integer, ForeignKey('roads.id'), primary_key=True),
)

road_end2_connections = Table(
    'road_end2_connections',
    Base.metadata,
    Column('road_id', Integer, ForeignKey('roads.id'), primary_key=True),
    Column('connected_road_id', Integer, ForeignKey('roads.id'), primary_key=True),
)


class Road(Base):
    __tablename__ = 'roads'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Geometry of the segment itself and of each of its two ends.
    geom: Mapped[object] = mapped_column(Geometry(geometry_type='LINESTRING', srid=SRID))
    end1: Mapped[object] = mapped_column(Geometry(geometry_type='POINT', srid=SRID))
    end2: Mapped[object] = mapped_column(Geometry(geometry_type='POINT', srid=SRID))

    # Roads meeting this one at end1 / at end2. Two separate many-to-many
    # relationships, one per end.
    end1_roads: Mapped[List['Road']] = relationship(
        'Road',
        secondary=road_end1_connections,
        primaryjoin=lambda: Road.id == road_end1_connections.c.road_id,
        secondaryjoin=lambda: Road.id == road_end1_connections.c.connected_road_id,
    )
    end2_roads: Mapped[List['Road']] = relationship(
        'Road',
        secondary=road_end2_connections,
        primaryjoin=lambda: Road.id == road_end2_connections.c.road_id,
        secondaryjoin=lambda: Road.id == road_end2_connections.c.connected_road_id,
    )
