from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import get_settings
from app.db.models import ActivationEvent, Machine, Relay, RelayEvent, RelaySchedule, SensorReading, SystemLog
from app.db.session import get_db
from app.schemas import (
    ActivationEventOut,
    DashboardStatusOut,
    HealthOut,
    MachineOut,
    MachineUpdate,
    ManualTriggerOut,
    RelayControllerInfoOut,
    RelayEventOut,
    RelayOut,
    RelayScheduleOut,
    RelayScheduleUpdate,
    RelaySetIn,
    RelayUpdate,
    RoomSummaryOut,
    SensorLatestOut,
    SensorReadingOut,
    SystemLogOut,
    DataSummaryOut,
)
from app.services.machine_controller import build_controller
from app.services.machine_service import get_last_activation, get_machine, reschedule_machine, seconds_until, trigger_machine
from app.services.relay_service import apply_state, list_relays, relay_history, toggle_relay
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


def serialize_relay(relay: Relay) -> RelayOut:
    return RelayOut(
        id=relay.id,
        name=relay.name,
        description=relay.description,
        bit_index=relay.bit_index,
        is_on=relay.is_on,
        enabled=relay.enabled,
        display_order=relay.display_order,
        last_changed_at=relay.last_changed_at,
    )


def serialize_relay_event(event: RelayEvent) -> RelayEventOut:
    return RelayEventOut(
        id=event.id,
        relay_id=event.relay_id,
        state=event.state,
        action=event.action,
        trigger_source=event.trigger_source,
        success=event.success,
        message=event.message,
        created_at=event.created_at,
    )


def public_relay_list(db: Session) -> list[RelayOut]:
    return [serialize_relay(r) for r in list_relays(db)]


def serialize_relay_schedule(sched: RelaySchedule) -> RelayScheduleOut:
    return RelayScheduleOut(
        relay_id=sched.relay_id,
        enabled=sched.enabled,
        on_duration_seconds=sched.on_duration_seconds,
        off_duration_seconds=sched.off_duration_seconds,
        next_run_at=sched.next_run_at,
        current_phase=sched.current_phase,
        updated_at=sched.updated_at,
    )


def list_relay_schedules(db: Session) -> list[RelayScheduleOut]:
    rows = db.execute(select(RelaySchedule).order_by(RelaySchedule.relay_id)).scalars()
    return [serialize_relay_schedule(s) for s in rows]


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
        relays=public_relay_list(db),
        relay_schedules=list_relay_schedules(db),
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
        relays=db.scalar(select(func.count()).select_from(Relay)) or 0,
        relay_events=db.scalar(select(func.count()).select_from(RelayEvent)) or 0,
    )


@router.get("/public/relays", response_model=list[RelayOut])
def public_relays(db: Session = Depends(get_db)) -> list[RelayOut]:
    return public_relay_list(db)


@router.get("/relays", response_model=list[RelayOut])
def admin_list_relays(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[RelayOut]:
    return public_relay_list(db)


@router.get("/relays/{relay_id}", response_model=RelayOut)
def admin_get_relay(
    relay_id: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    relay = db.get(Relay, relay_id)
    if relay is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    return serialize_relay(relay)


@router.post("/relays/{relay_id}/set", response_model=RelayOut)
def admin_set_relay(
    relay_id: str,
    payload: RelaySetIn,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    controller = getattr(request.app.state, "relay_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Relay controller is not initialized.")
    try:
        relay, _event = apply_state(db, relay_id, payload.on, controller, action="set", trigger_source="api")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_relay(relay)


@router.post("/relays/{relay_id}/on", response_model=RelayOut)
def admin_relay_on(
    relay_id: str,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    return admin_set_relay(relay_id, RelaySetIn(on=True), request, _, db)


@router.post("/relays/{relay_id}/off", response_model=RelayOut)
def admin_relay_off(
    relay_id: str,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    return admin_set_relay(relay_id, RelaySetIn(on=False), request, _, db)


@router.post("/relays/{relay_id}/toggle", response_model=RelayOut)
def admin_relay_toggle(
    relay_id: str,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    controller = getattr(request.app.state, "relay_controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Relay controller is not initialized.")
    try:
        relay, _event = toggle_relay(db, relay_id, controller, trigger_source="api")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_relay(relay)


@router.get("/relays/{relay_id}/events", response_model=list[RelayEventOut])
def admin_relay_events(
    relay_id: str,
    limit: int = Query(default=200, ge=1, le=5000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayEventOut]:
    return [serialize_relay_event(e) for e in relay_history(db, relay_id=relay_id, limit=limit)]


@router.get("/relay-events", response_model=list[RelayEventOut])
def admin_all_relay_events(
    limit: int = Query(default=500, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayEventOut]:
    return [serialize_relay_event(e) for e in relay_history(db, relay_id=None, limit=limit)]


@router.patch("/relays/{relay_id}", response_model=RelayOut)
def admin_update_relay(
    relay_id: str,
    payload: RelayUpdate,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    relay = db.get(Relay, relay_id)
    if relay is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    if payload.name is not None:
        relay.name = payload.name
    if payload.description is not None:
        relay.description = payload.description
    if payload.enabled is not None:
        relay.enabled = payload.enabled
    if payload.display_order is not None:
        relay.display_order = payload.display_order
    db.add(relay)
    db.commit()
    db.refresh(relay)
    return serialize_relay(relay)


@router.get("/relay-schedules", response_model=list[RelayScheduleOut])
def admin_list_relay_schedules(
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayScheduleOut]:
    return list_relay_schedules(db)


@router.get("/relays/{relay_id}/schedule", response_model=RelayScheduleOut)
def admin_get_relay_schedule(
    relay_id: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Relay, relay_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    sched = db.get(RelaySchedule, relay_id)
    if sched is None:
        sched = RelaySchedule(
            relay_id=relay_id,
            enabled=False,
            on_duration_seconds=60,
            off_duration_seconds=60,
            next_run_at=None,
            current_phase="off",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
    return serialize_relay_schedule(sched)


@router.patch("/relays/{relay_id}/schedule", response_model=RelayScheduleOut)
def admin_update_relay_schedule(
    relay_id: str,
    payload: RelayScheduleUpdate,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Relay, relay_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    sched = db.get(RelaySchedule, relay_id)
    if sched is None:
        sched = RelaySchedule(
            relay_id=relay_id,
            enabled=False,
            on_duration_seconds=60,
            off_duration_seconds=60,
            current_phase="off",
        )
        db.add(sched)

    if payload.on_duration_seconds is not None:
        sched.on_duration_seconds = payload.on_duration_seconds
    if payload.off_duration_seconds is not None:
        sched.off_duration_seconds = payload.off_duration_seconds
    if payload.enabled is not None:
        sched.enabled = payload.enabled

    db.commit()
    db.refresh(sched)

    scheduler = getattr(request.app.state, "relay_scheduler", None)
    if scheduler is not None:
        try:
            applied = scheduler.apply_schedule_change(db, relay_id)
            if applied is not None:
                sched = applied
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger("app.relay_scheduler").exception(
                "apply_schedule_change failed for %s", relay_id
            )
    return serialize_relay_schedule(sched)


@router.get("/relays-controller", response_model=RelayControllerInfoOut)
def admin_relay_controller_info(
    request: Request,
    _: str = Depends(require_admin),
) -> RelayControllerInfoOut:
    settings = get_settings()
    controller = getattr(request.app.state, "relay_controller", None)
    initialized = False
    latch = 0
    if controller is not None:
        latch = int(getattr(controller, "latch", 0)) & 0xFF
        # Mock controllers have no _configured flag; treat as always ready.
        initialized = bool(getattr(controller, "_configured", True))
    return RelayControllerInfoOut(
        mode=settings.relay_controller,
        active_high=settings.relay_active_high,
        board_num=settings.mcc_board_num,
        digital_port=settings.mcc_digital_port,
        bit_map=settings.relay_bit_map,
        initialized=initialized,
        latch=latch,
    )
