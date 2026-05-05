from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import get_settings
from app.db.models import ActivationEvent, Machine, SensorReading, SystemLog
from app.db.session import get_db
from app.schemas import (
    ActivationEventOut,
    DashboardStatusOut,
    HealthOut,
    MachineOut,
    MachineUpdate,
    ManualTriggerOut,
    RoomSummaryOut,
    SensorLatestOut,
    SensorReadingOut,
    SystemLogOut,
    DataSummaryOut,
)
from app.services.machine_controller import build_controller
from app.services.machine_service import get_last_activation, get_machine, reschedule_machine, seconds_until, trigger_machine
from app.services.sensor_service import latest_by_sensor, recent_readings

router = APIRouter()


def serialize_machine(machine: Machine) -> MachineOut:
    return MachineOut(
        id=machine.id,
        name=machine.name,
        enabled=machine.enabled,
        interval_seconds=machine.interval_seconds,
        activation_duration_seconds=machine.activation_duration_seconds,
        next_run_at=machine.next_run_at,
    )


def serialize_activation(event: ActivationEvent | None) -> ActivationEventOut | None:
    if event is None:
        return None
    return ActivationEventOut(
        id=event.id,
        machine_id=event.machine_id,
        started_at=event.started_at,
        completed_at=event.completed_at,
        status=event.status,
        trigger_source=event.trigger_source,
        message=event.message,
    )


def room_summary(db: Session) -> RoomSummaryOut:
    latest = latest_by_sensor(db)
    latest_out = [
        SensorLatestOut(
            sensor_name=reading.sensor_name,
            temperature=reading.temperature,
            relative_humidity=reading.relative_humidity,
            recorded_at=reading.recorded_at,
        )
        for reading in latest
    ]
    return RoomSummaryOut(
        latest_by_sensor=latest_out,
        average_temperature=round(mean([item.temperature for item in latest_out]), 2) if latest_out else None,
        average_relative_humidity=round(mean([item.relative_humidity for item in latest_out]), 2) if latest_out else None,
        sensor_count=len(latest_out),
    )


@router.get("/health", response_model=HealthOut)
def health(request: Request, db: Session = Depends(get_db)) -> HealthOut:
    db.execute(select(Machine).limit(1)).first()
    scheduler = getattr(request.app.state, "machine_scheduler", None)
    return HealthOut(status="ok", database="ok", scheduler_running=bool(scheduler and scheduler.running))


@router.get("/dashboard", response_model=DashboardStatusOut)
def dashboard(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> DashboardStatusOut:
    return dashboard_payload(db)


@router.get("/public/dashboard", response_model=DashboardStatusOut)
def public_dashboard(db: Session = Depends(get_db)) -> DashboardStatusOut:
    return dashboard_payload(db)


def dashboard_payload(db: Session) -> DashboardStatusOut:
    settings = get_settings()
    machine = get_machine(db, settings.default_machine_id)
    last_activation = get_last_activation(db, machine.id)
    return DashboardStatusOut(
        machine=serialize_machine(machine),
        last_activation=serialize_activation(last_activation),
        next_run_at=machine.next_run_at,
        seconds_until_next_run=seconds_until(machine.next_run_at),
        room=room_summary(db),
    )


@router.get("/machines/{machine_id}", response_model=MachineOut)
def read_machine(machine_id: str, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> MachineOut:
    try:
        return serialize_machine(get_machine(db, machine_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/machines", response_model=list[MachineOut])
def list_machines(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[MachineOut]:
    machines = db.execute(select(Machine).order_by(Machine.id)).scalars()
    return [serialize_machine(machine) for machine in machines]


@router.patch("/machines/{machine_id}", response_model=MachineOut)
def update_machine(
    machine_id: str,
    payload: MachineUpdate,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MachineOut:
    try:
        machine = get_machine(db, machine_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.enabled is not None:
        machine.enabled = payload.enabled
    if payload.interval_seconds is not None:
        machine.interval_seconds = payload.interval_seconds
    if payload.activation_duration_seconds is not None:
        machine.activation_duration_seconds = payload.activation_duration_seconds
    reschedule_machine(db, machine)
    return serialize_machine(machine)


@router.post("/machines/{machine_id}/trigger", response_model=ManualTriggerOut)
def manual_trigger(machine_id: str, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> ManualTriggerOut:
    settings = get_settings()
    controller = build_controller(settings)
    try:
        event = trigger_machine(db, machine_id, controller, trigger_source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ManualTriggerOut(event=serialize_activation(event))


@router.get("/machines/{machine_id}/activations", response_model=list[ActivationEventOut])
def activation_history(
    machine_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ActivationEventOut]:
    events = db.execute(
        select(ActivationEvent)
        .where(ActivationEvent.machine_id == machine_id)
        .order_by(desc(ActivationEvent.started_at))
        .limit(limit)
    ).scalars()
    return [serialize_activation(event) for event in events if event is not None]


@router.get("/activations", response_model=list[ActivationEventOut])
def all_activation_history(
    limit: int = Query(default=500, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ActivationEventOut]:
    events = db.execute(select(ActivationEvent).order_by(desc(ActivationEvent.started_at)).limit(limit)).scalars()
    return [serialize_activation(event) for event in events if event is not None]


@router.get("/sensors/latest", response_model=RoomSummaryOut)
def latest_sensors(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> RoomSummaryOut:
    return room_summary(db)


@router.get("/sensors/readings", response_model=list[SensorReadingOut])
def sensor_readings(
    sensor_name: str | None = None,
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=1000, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[SensorReadingOut]:
    return sensor_readings_payload(db, sensor_name=sensor_name, hours=hours, limit=limit)


@router.get("/public/sensors/readings", response_model=list[SensorReadingOut])
def public_sensor_readings(
    sensor_name: str | None = None,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[SensorReadingOut]:
    return sensor_readings_payload(db, sensor_name=sensor_name, hours=hours, limit=limit)


def sensor_readings_payload(
    db: Session,
    sensor_name: str | None = None,
    hours: int = 24,
    limit: int = 1000,
) -> list[SensorReadingOut]:
    readings = recent_readings(db, sensor_name=sensor_name, hours=hours, limit=limit)
    return [
        SensorReadingOut(
            id=reading.id,
            sensor_name=reading.sensor_name,
            temperature=reading.temperature,
            relative_humidity=reading.relative_humidity,
            recorded_at=reading.recorded_at,
            raw_payload=reading.raw_payload,
        )
        for reading in reversed(readings)
    ]


@router.get("/sensors/names", response_model=list[str])
def sensor_names(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[str]:
    names = db.execute(select(SensorReading.sensor_name).distinct().order_by(SensorReading.sensor_name)).scalars()
    return list(names)


@router.get("/logs", response_model=list[SystemLogOut])
def system_logs(
    limit: int = Query(default=500, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[SystemLogOut]:
    logs = db.execute(select(SystemLog).order_by(desc(SystemLog.created_at)).limit(limit)).scalars()
    return [
        SystemLogOut(
            id=log.id,
            level=log.level,
            component=log.component,
            message=log.message,
            created_at=log.created_at,
        )
        for log in logs
    ]


@router.get("/data/summary", response_model=DataSummaryOut)
def data_summary(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> DataSummaryOut:
    return DataSummaryOut(
        machines=db.scalar(select(func.count()).select_from(Machine)) or 0,
        activation_events=db.scalar(select(func.count()).select_from(ActivationEvent)) or 0,
        sensor_readings=db.scalar(select(func.count()).select_from(SensorReading)) or 0,
        system_logs=db.scalar(select(func.count()).select_from(SystemLog)) or 0,
    )
