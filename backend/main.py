from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import disaster, evacuation_point
from database import init_db
from services.registry import init_services


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data model tables exist in PostGIS before serving requests.
    init_db()
    # Construct and register the service singletons (alarm, notification, ...).
    init_services()
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(disaster.router)
app.include_router(evacuation_point.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
