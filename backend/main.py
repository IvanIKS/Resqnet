from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.registry import init_services
from database import SessionLocal
from seed import seed
from api import disaster, evacuation_point, evacuation_route,  live_map
from database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data model tables exist in PostGIS before serving requests.
    init_db()
    # Construct and register the service singletons (alarm, notification, ...).
    init_services()
    with SessionLocal() as session:
        seed(session)
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(disaster.router)
app.include_router(evacuation_point.router)
app.include_router(live_map.router)
app.include_router(evacuation_route.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
