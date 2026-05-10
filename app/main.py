from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import router
from app.auth import require_admin
from app.config import get_settings
from app.db.init_db import (
    ensure_default_collector,
    ensure_default_machine,
    ensure_default_relay_schedules,
    ensure_default_relays,
    init_db,
)
from app.db.session import SessionLocal
from app.services.collector_agent import CollectorAgent
from app.services.machine_controller import build_controller
from app.services.relay_controller import build_relay_controller
from app.services.relay_scheduler import RelayScheduler
from app.services.scheduler import MachineScheduler
from app.services.sensor_service import SensorDevice, SensorIngestionManager


settings = get_settings()
controller = build_controller(settings)

# Hub-only deployments don't touch local hardware. Keep relay_controller None
# so failed hardware writes can't happen and the API knows to enqueue commands.
if settings.runs_local_hardware:
    relay_controller = build_relay_controller(settings)
    machine_scheduler = MachineScheduler(settings, controller)
    relay_scheduler = RelayScheduler(relay_controller, machine_key=settings.collector_id)
    sensor_manager = SensorIngestionManager(
        devices=[
            SensorDevice(settings.arduino_1_name, settings.arduino_1_port),
            SensorDevice(settings.arduino_2_name, settings.arduino_2_port),
        ],
        baudrate=settings.arduino_baudrate,
        timeout_seconds=settings.sensor_read_timeout_seconds,
        simulator=settings.sensor_simulator,
        machine_key=settings.collector_id,
    )
else:
    relay_controller = None
    machine_scheduler = None
    relay_scheduler = None
    sensor_manager = None

# Collector agent runs on the lab machine (collector mode). In all_in_one mode
# it is unnecessary because hub and hardware live in the same process.
if settings.app_mode == "collector":
    collector_agent: CollectorAgent | None = CollectorAgent(
        settings, relay_controller, relay_scheduler
    )
else:
    collector_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_default_machine(db)
        ensure_default_relays(db)
        # Only seed schedules + a local collector entry when this process
        # actually owns hardware. A pure hub starts empty and only gets
        # machines as collectors register.
        if settings.runs_local_hardware:
            ensure_default_relay_schedules(db, machine_key=settings.collector_id)
            ensure_default_collector(db)

    if relay_controller is not None:
        try:
            relay_controller.initialize()
        except Exception as exc:
            logging.getLogger("app.relay").warning(
                "Relay controller initialize failed: %s", exc
            )

    app.state.machine_scheduler = machine_scheduler
    app.state.sensor_manager = sensor_manager
    app.state.relay_controller = relay_controller
    app.state.relay_scheduler = relay_scheduler
    app.state.collector_agent = collector_agent

    if machine_scheduler is not None:
        machine_scheduler.start()
    if relay_scheduler is not None:
        relay_scheduler.start()
    if sensor_manager is not None:
        sensor_manager.start()
    if collector_agent is not None:
        collector_agent.start()
    yield
    if collector_agent is not None:
        collector_agent.stop()
    if sensor_manager is not None:
        sensor_manager.stop()
    if relay_scheduler is not None:
        relay_scheduler.stop()
    if machine_scheduler is not None:
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
