from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

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
from app.services.relay_activation import RelayActivator
from app.services.relay_controller import build_relay_controller, safe_all_off
from app.services.relay_scheduler import RelayScheduler
from app.services.scheduler import MachineScheduler
from app.services.sensor_service import SensorDevice, SensorIngestionManager


STATIC_DIR = Path(__file__).resolve().parent / "static"

settings = get_settings()
controller = build_controller(settings)

# Hub-only deployments don't touch local hardware. Keep relay_controller None
# so failed hardware writes can't happen and the API knows to enqueue commands.
if settings.runs_local_hardware:
    relay_controller = build_relay_controller(settings)
    machine_scheduler = MachineScheduler(settings, controller)
    relay_scheduler = RelayScheduler(
        relay_controller,
        machine_key=settings.collector_id,
        max_activation_seconds=settings.relay_max_activation_seconds,
    )
    relay_activator: RelayActivator | None = RelayActivator(
        relay_controller,
        machine_key=settings.collector_id,
        max_duration_seconds=settings.relay_max_activation_seconds,
    )
    sensor_manager = SensorIngestionManager(
        devices=[
            SensorDevice(settings.arduino_1_name, settings.arduino_1_port, settings.arduino_1_chamber_id),
            SensorDevice(settings.arduino_2_name, settings.arduino_2_port, settings.arduino_2_chamber_id),
        ],
        baudrate=settings.arduino_baudrate,
        timeout_seconds=settings.sensor_read_timeout_seconds,
        simulator=settings.sensor_simulator,
        machine_key=settings.collector_id,
        reconnect_delay_seconds=settings.sensor_reconnect_delay_seconds,
    )
else:
    relay_controller = None
    machine_scheduler = None
    relay_scheduler = None
    relay_activator = None
    sensor_manager = None

# Collector agent runs on the lab machine (collector mode). In all_in_one mode
# it is unnecessary because hub and hardware live in the same process.
if settings.app_mode == "collector":
    collector_agent: CollectorAgent | None = CollectorAgent(
        settings, relay_controller, relay_scheduler
    )
else:
    collector_agent = None


logger = logging.getLogger("app.lifecycle")


def _startup(app: FastAPI) -> None:
    """Bring the process up in hardware-safe order.

    Relays are de-energised before anything that could fire them is started,
    and schedules are reloaded before the tick loop runs, so a crash mid-cycle
    cannot leave a relay on across a restart.
    """
    # 1. Config + database.
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

    # 2. Relay controller, then 3. every relay off.
    if relay_controller is not None:
        try:
            relay_controller.initialize()
        except Exception as exc:
            # On Linux/dev the MCC path raises (no mcculw). Startup continues;
            # relay writes will fail loudly instead of silently half-working.
            logger.warning("Relay controller initialize failed: %s", exc)
        else:
            safe_all_off(relay_controller, "startup")

    # 4. Load persisted schedules (also forces a safe relay state).
    if relay_scheduler is not None:
        try:
            relay_scheduler.load_schedules()
        except Exception:
            logger.exception("loading relay schedules failed")

    app.state.machine_scheduler = machine_scheduler
    app.state.sensor_manager = sensor_manager
    app.state.relay_controller = relay_controller
    app.state.relay_scheduler = relay_scheduler
    app.state.relay_activator = relay_activator
    app.state.collector_agent = collector_agent

    # 5. Arduino readers, 6. relay scheduler, 7. sync/heartbeat.
    if sensor_manager is not None:
        sensor_manager.start()
    if machine_scheduler is not None:
        machine_scheduler.start()
    if relay_scheduler is not None:
        relay_scheduler.start()
    if collector_agent is not None:
        collector_agent.start()


def _shutdown() -> None:
    """Stop schedulers first, de-energise, then stop readers and sync."""
    if relay_scheduler is not None:
        relay_scheduler.stop()
    if machine_scheduler is not None:
        machine_scheduler.stop()
    if relay_activator is not None:
        relay_activator.all_off("shutdown")
    else:
        safe_all_off(relay_controller, "shutdown")
    if sensor_manager is not None:
        sensor_manager.stop()
    if collector_agent is not None:
        collector_agent.stop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _startup(app)
    except Exception:
        # Partial startup must not leave energised relays behind.
        safe_all_off(relay_controller, "failed startup")
        raise
    try:
        yield
    finally:
        _shutdown()


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
    return FileResponse(STATIC_DIR / "public.html")


@app.get("/public", include_in_schema=False)
def public_dashboard_page():
    return FileResponse(STATIC_DIR / "public.html")


@app.get("/admin", include_in_schema=False)
def sysadmin_dashboard(_: str = Depends(require_admin)):
    return FileResponse(STATIC_DIR / "index.html")
