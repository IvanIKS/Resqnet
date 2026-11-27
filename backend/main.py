from contextlib import asynccontextmanager

from fastapi import FastAPI

from database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data model tables exist in PostGIS before serving requests.
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"status": "ok"}
