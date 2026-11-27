"""Map ingestion: satellite flood layers and the OSM road graph into PostGIS.

The real work lives in two modules:
  * service.ingestion_flood: Copernicus EMS (EMSR444 AOI02) -> disaster_areas
  * service.ingestion_roads: OSM drivable roads -> roads + road_end*_connections
Each can also be run on its own: ``python -m service.ingestion_flood`` /
``python -m service.ingestion_roads`` from ``backend/``.
"""

from service import ingestion_flood, ingestion_roads


class IngestionService:
    def __init__(self, session_factory=None):
        if session_factory is None:
            from database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory

    def load_satellite_data(self, zip_path=ingestion_flood.DEFAULT_ZIP):
        """Read the EMSR package and store observed flood polygons."""
        records = ingestion_flood.read_package(zip_path)
        return self.ingest_into_database(records)

    def ingest_into_database(self, records):
        with self.session_factory() as session:
            return ingestion_flood.write_to_db(session, records)

    def load_roads(self, buffer=ingestion_roads.DEFAULT_BUFFER, replace=True):
        """Download the OSM road graph around AOI02 and store roads + junctions."""
        network = ingestion_roads.transform_graph(ingestion_roads.download_graph(buffer))
        with self.session_factory.begin() as session:
            ingestion_roads.write_roads(session, network, replace=replace)
        return network
