from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router
from app.auth import require_admin
from app.config import get_settings
from app.db.init_db import ensure_default_machine, ensure_default_relays, init_db
from app.db.session import SessionLocal
from app.services.machine_controller import build_controller
from app.services.relay_controller import build_relay_controller
from app.services.scheduler import MachineScheduler
from app.services.sensor_service import SensorDevice, SensorIngestionManager


settings = get_settings()
controller = build_controller(settings)
relay_controller = build_relay_controller(settings)
machine_scheduler = MachineScheduler(settings, controller)
sensor_manager = SensorIngestionManager(
    devices=[
        SensorDevice(settings.arduino_1_name, settings.arduino_1_port),
        SensorDevice(settings.arduino_2_name, settings.arduino_2_port),
    ],
    baudrate=settings.arduino_baudrate,
    timeout_seconds=settings.sensor_read_timeout_seconds,
    simulator=settings.sensor_simulator,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_default_machine(db)
        ensure_default_relays(db)
    try:
        relay_controller.initialize()
    except Exception as exc:
        # Don't crash the app if hardware/library is unavailable; surface in logs.
        import logging

        logging.getLogger("app.relay").warning("Relay controller initialize failed: %s", exc)
    app.state.machine_scheduler = machine_scheduler
    app.state.sensor_manager = sensor_manager
    app.state.relay_controller = relay_controller
    machine_scheduler.start()
    sensor_manager.start()
    yield
    sensor_manager.stop()
    machine_scheduler.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials="*" not in settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")


@app.get("/", include_in_schema=False)
def root_dashboard():
    return FileResponse("app/static/public.html")


@app.get("/public", include_in_schema=False)
def public_dashboard_page():
    return FileResponse("app/static/public.html")


@app.get("/admin", include_in_schema=False)
def sysadmin_dashboard(_: str = Depends(require_admin)):
    return FileResponse("app/static/index.html")
