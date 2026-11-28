from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import auth, disaster, evacuation_point
from database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data model tables exist in PostGIS before serving requests.
    init_db()
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(auth.router)
app.include_router(disaster.router)
app.include_router(evacuation_point.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
