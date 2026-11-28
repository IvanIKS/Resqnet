from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.registry import init_services
from api import disaster, evacuation_point, live_map
from database import SessionLocal, init_db
from seed import seed


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


@app.get("/api/health")
def health():
    return {"status": "ok"}
