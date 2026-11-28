from contextlib import asynccontextmanager

from fastapi import FastAPI

from services.registry import init_services
from database import SessionLocal
from seed import seed
from api import auth, disaster, evacuation_point, evacuation_route, live_map, people_estimate, field_report
from database import init_db
from services import osnma


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the data model tables exist in PostGIS before serving requests.
    init_db()
    # Construct and register the service singletons (alarm, notification, ...).
    init_services()
    with SessionLocal() as session:
        seed(session)
    # OSNMA verdicts of the recorded samples take seconds; compute them off the request path.
    osnma.warm_up()
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(auth.router)
app.include_router(disaster.router)
app.include_router(evacuation_point.router)
app.include_router(live_map.router)
app.include_router(evacuation_route.router)
app.include_router(field_report.router)
app.include_router(people_estimate.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
