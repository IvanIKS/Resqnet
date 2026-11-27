from datetime import datetime
from typing import List

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, Column, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase
from sqlalchemy.sql.schema import ForeignKey
from sqlalchemy.sql.sqltypes import BigInteger, Boolean, Integer, String, DateTime

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


# disaster_areas.kind of the polygons written by service.ingestion_flood.
AREA_KIND_OBSERVED_FLOOD = 'observed_flood'  # satellite delineation, e.g. EMS observedEventA


class DisasterArea(Base):
    __tablename__ = 'disaster_areas'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    disaster_id: Mapped[int] = mapped_column(Integer, ForeignKey('disasters.id'))
    area: Mapped[object] = mapped_column(Geometry(geometry_type='POLYGON', srid=SRID))
    # Observation metadata, set by service.ingestion_flood. Rows are only ever
    # appended, never updated, so the history of what was observed stays. All
    # nullable: rows from POST /disasterArea and rows from before these columns
    # have none of them, and such rows never count as current.
    kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # AREA_KIND_*
    # Acquisition time of the image the polygon was mapped from, not the load
    # time. NULL = unknown.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Provider and image, e.g. "CEMS EMSR444 AOI02: SPOT-6-7 (6 m, Post-event)",
    # and the product id, e.g. "EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1".
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The row counts at moment T only while observed_at <= T < valid_until.
    # NULL (always so when observed_at is NULL): never current, i.e. stale.
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the row was written to this database.
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Coverage kinds. Neither is flood: they say where a missing flood polygon
# means "observed dry" and where it means "nobody looked".
COVERAGE_KIND_ANALYSIS_EXTENT = 'analysis_extent'  # area the product analysed (EMS areaOfInterestA)
COVERAGE_KIND_NOT_ANALYSED = 'not_analysed'  # inside the extent but not analysed (EMS "Not Analysed")


class AnalysisCoverage(Base):
    """Where a satellite product looked, and where it could not.

    Written by service.ingestion_flood next to the product's flood polygons,
    with the same observation metadata, append-only. A missing flood polygon
    means "observed dry" only inside a current analysis_extent and outside its
    not_analysed areas; everywhere else the flood state is unknown.
    """
    __tablename__ = 'analysis_coverage'
    __table_args__ = (
        # A misspelt kind would silently drop a "not analysed" area and make
        # the ground under it look observed.
        CheckConstraint(
            f"kind IN ('{COVERAGE_KIND_ANALYSIS_EXTENT}', '{COVERAGE_KIND_NOT_ANALYSED}')",
            name='analysis_coverage_kind_check',
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    disaster_id: Mapped[int] = mapped_column(Integer, ForeignKey('disasters.id'))
    kind: Mapped[str] = mapped_column(String(32))
    area: Mapped[object] = mapped_column(Geometry(geometry_type='MULTIPOLYGON', srid=SRID))
    # Same meaning as on DisasterArea.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product: Mapped[str | None] = mapped_column(String(128), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    # Indexed: the primary key leads with road_id, so without this every
    # DELETE FROM roads scans the whole table for the FK check.
    Column('connected_road_id', Integer, ForeignKey('roads.id'), primary_key=True, index=True),
)

road_end2_connections = Table(
    'road_end2_connections',
    Base.metadata,
    Column('road_id', Integer, ForeignKey('roads.id'), primary_key=True),
    # Indexed: the primary key leads with road_id, so without this every
    # DELETE FROM roads scans the whole table for the FK check.
    Column('connected_road_id', Integer, ForeignKey('roads.id'), primary_key=True, index=True),
)


class Road(Base):
    __tablename__ = 'roads'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Geometry of the segment itself and of each of its two ends.
    geom: Mapped[object] = mapped_column(Geometry(geometry_type='LINESTRING', srid=SRID))
    end1: Mapped[object] = mapped_column(Geometry(geometry_type='POINT', srid=SRID))
    end2: Mapped[object] = mapped_column(Geometry(geometry_type='POINT', srid=SRID))

    # OSM endpoint node IDs and comma-separated way IDs from the simplified edge.
    osm_u: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    osm_v: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    osm_way_ids: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set by service.ingestion_roads from OSM. True: traffic may only go
    # end1 -> end2. False: both ways. NULL: unknown, not two-way.
    oneway: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

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
