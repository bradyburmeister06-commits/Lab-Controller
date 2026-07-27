from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import require_admin, require_collector_token
from app.config import get_settings, is_valid_machine_key
from app.db.models import (
    ActivationEvent,
    Collector,
    Machine,
    Relay,
    RelayEvent,
    RelaySchedule,
    SensorReading,
    SystemLog,
)
from app.db.init_db import ensure_machine_schedules
from app.db.session import get_db
from app.schemas import (
    ActivationEventOut,
    CollectorCommandAckIn,
    CollectorCommandOut,
    CollectorHeartbeatIn,
    CollectorOut,
    CollectorPollOut,
    CollectorRegisterIn,
    CollectorRelayBatchIn,
    CollectorSensorBatchIn,
    CollectorUpdate,
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
from app.services import collector_hub
from app.services.collector_hub import InvalidMachineKey, validate_machine_key
from app.services.machine_controller import build_controller
from app.services.machine_service import get_last_activation, get_machine, reschedule_machine, seconds_until, trigger_machine
from app.services.relay_service import apply_state, list_relays, relay_history, toggle_relay
from app.services.sensor_service import latest_by_sensor, recent_readings

router = APIRouter()


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _stale_after_seconds() -> int:
    return get_settings().collector_stale_after_seconds


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
        machine_key=event.machine_key,
    )


def public_relay_list(db: Session) -> list[RelayOut]:
    return [serialize_relay(r) for r in list_relays(db)]


def serialize_relay_schedule(sched: RelaySchedule) -> RelayScheduleOut:
    return RelayScheduleOut(
        machine_key=sched.machine_key,
        relay_id=sched.relay_id,
        enabled=sched.enabled,
        on_duration_seconds=sched.on_duration_seconds,
        off_duration_seconds=sched.off_duration_seconds,
        next_run_at=sched.next_run_at,
        current_phase=sched.current_phase,
        updated_at=sched.updated_at,
    )


def list_relay_schedules(db: Session, machine_key: str | None = None) -> list[RelayScheduleOut]:
    if machine_key is not None:
        rows = collector_hub.list_schedules_for_machine(db, machine_key)
    else:
        rows = collector_hub.list_all_schedules(db)
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


def _serialize_collector(collector: Collector) -> CollectorOut:
    threshold = _stale_after_seconds()
    online = collector_hub.collector_is_online(collector, threshold_seconds=threshold)
    stale = not online
    return CollectorOut(
        id=collector.id,
        machine_key=collector.id,
        name=collector.display_name,
        display_name=collector.display_name,
        role=collector.role or "collector",
        status="online" if online else "stale" if collector.last_heartbeat_at else "unknown",
        is_enabled=bool(collector.is_enabled),
        mode=collector.mode,
        host=collector.host,
        hostname=collector.hostname,
        last_seen_ip=collector.last_seen_ip,
        software_version=collector.software_version,
        last_heartbeat_at=collector.last_heartbeat_at,
        last_status_message=collector.last_status_message,
        relay_controller_mode=collector.relay_controller_mode,
        relay_controller_initialized=bool(collector.relay_controller_initialized),
        runtime_state=collector.runtime_state,
        online=online,
        stale=stale,
        seconds_since_heartbeat=collector_hub.seconds_since_heartbeat(collector),
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
    settings = get_settings()
    scheduler = getattr(request.app.state, "machine_scheduler", None)
    sensor_manager = getattr(request.app.state, "sensor_manager", None)
    relay_scheduler = getattr(request.app.state, "relay_scheduler", None)
    collector_agent = getattr(request.app.state, "collector_agent", None)
    return HealthOut(
        status="ok",
        database="ok",
        scheduler_running=bool(scheduler and scheduler.running),
        app_mode=settings.app_mode,
        collector_id=settings.collector_id,
        collector_agent_running=bool(collector_agent and collector_agent.running) if collector_agent is not None else None,
        relay_controller_mode=settings.relay_controller,
        sensor_manager_running=bool(sensor_manager and sensor_manager.running) if sensor_manager is not None else None,
        relay_scheduler_running=bool(relay_scheduler and relay_scheduler.running) if relay_scheduler is not None else None,
        hub_base_url=settings.hub_base_url if settings.app_mode == "collector" else None,
    )


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
    collectors = [_serialize_collector(c) for c in collector_hub.list_collectors(db)]
    return DashboardStatusOut(
        machine=serialize_machine(machine),
        last_activation=serialize_activation(last_activation),
        next_run_at=machine.next_run_at,
        seconds_until_next_run=seconds_until(machine.next_run_at),
        room=room_summary(db),
        relays=public_relay_list(db),
        relay_schedules=list_relay_schedules(db),
        collectors=collectors,
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
    machine_key: str | None = None,
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=1000, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[SensorReadingOut]:
    return sensor_readings_payload(
        db, sensor_name=sensor_name, hours=hours, limit=limit, machine_key=machine_key
    )


@router.get("/public/sensors/readings", response_model=list[SensorReadingOut])
def public_sensor_readings(
    sensor_name: str | None = None,
    machine_key: str | None = None,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[SensorReadingOut]:
    return sensor_readings_payload(
        db, sensor_name=sensor_name, hours=hours, limit=limit, machine_key=machine_key
    )


def sensor_readings_payload(
    db: Session,
    sensor_name: str | None = None,
    hours: int = 24,
    limit: int = 1000,
    machine_key: str | None = None,
) -> list[SensorReadingOut]:
    readings = recent_readings(
        db, sensor_name=sensor_name, hours=hours, limit=limit, machine_key=machine_key
    )
    return [
        SensorReadingOut(
            id=reading.id,
            sensor_name=reading.sensor_name,
            temperature=reading.temperature,
            relative_humidity=reading.relative_humidity,
            recorded_at=reading.recorded_at,
            raw_payload=reading.raw_payload,
            machine_key=reading.machine_key,
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
        collectors=db.scalar(select(func.count()).select_from(Collector)) or 0,
    )


@router.get("/public/relays", response_model=list[RelayOut])
def public_relays(db: Session = Depends(get_db)) -> list[RelayOut]:
    return public_relay_list(db)


@router.get("/public/collectors", response_model=list[CollectorOut])
def public_collectors(db: Session = Depends(get_db)) -> list[CollectorOut]:
    """Read-only multi-machine summary for the public dashboard."""
    return [_serialize_collector(c) for c in collector_hub.list_collectors(db)]


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


def _enqueue_relay_set(db: Session, collector_id: str, relay_id: str, on: bool) -> Relay:
    relay = db.get(Relay, relay_id)
    if relay is None:
        raise ValueError(f"Unknown relay_id: {relay_id}")
    collector_hub.enqueue_command(
        db,
        collector_id=collector_id,
        command_type="relay_set",
        relay_id=relay_id,
        payload="on" if on else "off",
    )
    return relay


def _resolve_target_collector_id(payload_machine_key: str | None) -> str:
    settings = get_settings()
    if payload_machine_key:
        return payload_machine_key
    return settings.collector_id


@router.post("/relays/{relay_id}/set", response_model=RelayOut)
def admin_set_relay(
    relay_id: str,
    payload: RelaySetIn,
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    settings = get_settings()
    controller = getattr(request.app.state, "relay_controller", None)
    target_collector = _resolve_target_collector_id(machine_key)
    if controller is None or (machine_key and machine_key != settings.collector_id):
        if settings.app_mode == "hub" or machine_key:
            try:
                relay = _enqueue_relay_set(db, target_collector, relay_id, payload.on)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return serialize_relay(relay)
        raise HTTPException(status_code=503, detail="Relay controller is not initialized.")
    try:
        relay, _event = apply_state(
            db, relay_id, payload.on, controller,
            action="set", trigger_source="api", machine_key=settings.collector_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_relay(relay)


@router.post("/relays/{relay_id}/on", response_model=RelayOut)
def admin_relay_on(
    relay_id: str,
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    return admin_set_relay(relay_id, RelaySetIn(on=True), request, machine_key, _, db)


@router.post("/relays/{relay_id}/off", response_model=RelayOut)
def admin_relay_off(
    relay_id: str,
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    return admin_set_relay(relay_id, RelaySetIn(on=False), request, machine_key, _, db)


@router.post("/relays/{relay_id}/toggle", response_model=RelayOut)
def admin_relay_toggle(
    relay_id: str,
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayOut:
    settings = get_settings()
    controller = getattr(request.app.state, "relay_controller", None)
    target_collector = _resolve_target_collector_id(machine_key)
    if controller is None or (machine_key and machine_key != settings.collector_id):
        if settings.app_mode == "hub" or machine_key:
            relay = db.get(Relay, relay_id)
            if relay is None:
                raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
            collector_hub.enqueue_command(
                db,
                collector_id=target_collector,
                command_type="relay_toggle",
                relay_id=relay_id,
            )
            return serialize_relay(relay)
        raise HTTPException(status_code=503, detail="Relay controller is not initialized.")
    try:
        relay, _event = toggle_relay(
            db, relay_id, controller, trigger_source="api", machine_key=settings.collector_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_relay(relay)


@router.get("/relays/{relay_id}/events", response_model=list[RelayEventOut])
def admin_relay_events(
    relay_id: str,
    machine_key: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayEventOut]:
    return [serialize_relay_event(e) for e in relay_history(db, relay_id=relay_id, limit=limit, machine_key=machine_key)]


@router.get("/relay-events", response_model=list[RelayEventOut])
def admin_all_relay_events(
    machine_key: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayEventOut]:
    return [serialize_relay_event(e) for e in relay_history(db, relay_id=None, limit=limit, machine_key=machine_key)]


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


# --- Per-machine relay schedule APIs ----------------------------------------


def _resolve_schedule_machine_key(machine_key: str | None) -> str:
    settings = get_settings()
    return machine_key or settings.collector_id


def _ensure_schedule_row(
    db: Session, machine_key: str, relay_id: str, *, create: bool = True
) -> RelaySchedule | None:
    sched = db.get(RelaySchedule, (machine_key, relay_id))
    if sched is None and create:
        sched = RelaySchedule(
            machine_key=machine_key,
            relay_id=relay_id,
            enabled=False,
            on_duration_seconds=60,
            off_duration_seconds=60,
            current_phase="off",
        )
        db.add(sched)
        db.commit()
        db.refresh(sched)
    return sched


@router.get("/relay-schedules", response_model=list[RelayScheduleOut])
def admin_list_relay_schedules(
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayScheduleOut]:
    if machine_key is not None:
        return list_relay_schedules(db, machine_key=machine_key)
    # Backward compatibility: when no machine_key is provided, return one row
    # per relay using the local default machine_key. This keeps single-machine
    # callers (and existing tests) working.
    settings = get_settings()
    return list_relay_schedules(db, machine_key=settings.collector_id)


@router.get("/relays/{relay_id}/schedule", response_model=RelayScheduleOut)
def admin_get_relay_schedule(
    relay_id: str,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Relay, relay_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    key = _resolve_schedule_machine_key(machine_key)
    sched = _ensure_schedule_row(db, key, relay_id)
    return serialize_relay_schedule(sched)


@router.patch("/relays/{relay_id}/schedule", response_model=RelayScheduleOut)
def admin_update_relay_schedule(
    relay_id: str,
    payload: RelayScheduleUpdate,
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Relay, relay_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    key = _resolve_schedule_machine_key(machine_key)
    sched = _ensure_schedule_row(db, key, relay_id)

    if payload.on_duration_seconds is not None:
        sched.on_duration_seconds = payload.on_duration_seconds
    if payload.off_duration_seconds is not None:
        sched.off_duration_seconds = payload.off_duration_seconds
    if payload.enabled is not None:
        sched.enabled = payload.enabled

    db.commit()
    db.refresh(sched)

    settings = get_settings()
    scheduler = getattr(request.app.state, "relay_scheduler", None)
    is_local = key == settings.collector_id
    if scheduler is not None and is_local:
        try:
            applied = scheduler.apply_schedule_change(db, relay_id, machine_key=key)
            if applied is not None:
                sched = applied
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger("app.relay_scheduler").exception(
                "apply_schedule_change failed for %s/%s", key, relay_id
            )
    else:
        # Hub or remote target: enqueue a notification so the right collector
        # re-reads its scoped schedules immediately.
        collector_hub.enqueue_command(
            db,
            collector_id=key,
            command_type="schedule_changed",
            relay_id=relay_id,
        )
    return serialize_relay_schedule(sched)


@router.get("/admin/machines/{machine_key}/relay-schedules", response_model=list[RelayScheduleOut])
def admin_machine_relay_schedules(
    machine_key: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RelayScheduleOut]:
    if db.get(Collector, machine_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    return list_relay_schedules(db, machine_key=machine_key)


@router.get("/admin/machines/{machine_key}/relay-schedules/{relay_id}", response_model=RelayScheduleOut)
def admin_machine_get_relay_schedule(
    machine_key: str,
    relay_id: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Collector, machine_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    if db.get(Relay, relay_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown relay_id: {relay_id}")
    sched = _ensure_schedule_row(db, machine_key, relay_id)
    return serialize_relay_schedule(sched)


@router.patch("/admin/machines/{machine_key}/relay-schedules/{relay_id}", response_model=RelayScheduleOut)
def admin_machine_update_relay_schedule(
    machine_key: str,
    relay_id: str,
    payload: RelayScheduleUpdate,
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayScheduleOut:
    if db.get(Collector, machine_key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    return admin_update_relay_schedule(
        relay_id=relay_id,
        payload=payload,
        request=request,
        machine_key=machine_key,
        _=_,
        db=db,
    )


@router.get("/relays-controller", response_model=RelayControllerInfoOut)
def admin_relay_controller_info(
    request: Request,
    machine_key: str | None = Query(default=None),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RelayControllerInfoOut:
    settings = get_settings()
    controller = getattr(request.app.state, "relay_controller", None)
    initialized = False
    latch = 0
    mode = settings.relay_controller
    target_id = machine_key or settings.collector_id
    if controller is not None and target_id == settings.collector_id:
        latch = int(getattr(controller, "latch", 0)) & 0xFF
        initialized = bool(getattr(controller, "_configured", True))
    else:
        # Surface the remote collector's last reported relay-controller status.
        collector = db.get(Collector, target_id)
        if collector is not None:
            initialized = bool(collector.relay_controller_initialized)
            if collector.relay_controller_mode:
                mode = collector.relay_controller_mode
    return RelayControllerInfoOut(
        mode=mode,
        active_high=settings.relay_active_high,
        board_num=settings.mcc_board_num,
        digital_port=settings.mcc_digital_port,
        bit_map=settings.relay_bit_map,
        initialized=initialized,
        latch=latch,
    )


# --- Collector status (admin) and ingestion/poll (collector) endpoints ---


@router.get("/collectors", response_model=list[CollectorOut])
def admin_list_collectors(
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[CollectorOut]:
    return [_serialize_collector(c) for c in collector_hub.list_collectors(db)]


@router.get("/admin/machines", response_model=list[CollectorOut])
def admin_list_machines(
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[CollectorOut]:
    """Persistent multi-machine registry. The hub no longer reads this list
    from environment configuration."""
    return [_serialize_collector(c) for c in collector_hub.list_collectors(db)]


@router.get("/collectors/{collector_id}", response_model=CollectorOut)
def admin_get_collector(
    collector_id: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CollectorOut:
    collector = db.get(Collector, collector_id)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"Unknown collector_id: {collector_id}")
    return _serialize_collector(collector)


@router.get("/admin/machines/{machine_key}", response_model=CollectorOut)
def admin_get_machine(
    machine_key: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CollectorOut:
    collector = db.get(Collector, machine_key)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    return _serialize_collector(collector)


@router.patch("/admin/machines/{machine_key}", response_model=CollectorOut)
def admin_update_machine(
    machine_key: str,
    payload: CollectorUpdate,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CollectorOut:
    collector = db.get(Collector, machine_key)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    if payload.display_name is not None:
        collector.display_name = payload.display_name
    if payload.is_enabled is not None:
        collector.is_enabled = bool(payload.is_enabled)
    if payload.role is not None:
        collector.role = payload.role
    db.commit()
    db.refresh(collector)
    return _serialize_collector(collector)


@router.post("/admin/machines/{machine_key}/disable", response_model=CollectorOut)
def admin_disable_machine(
    machine_key: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CollectorOut:
    collector = db.get(Collector, machine_key)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    collector.is_enabled = False
    db.commit()
    db.refresh(collector)
    return _serialize_collector(collector)


@router.post("/admin/machines/{machine_key}/enable", response_model=CollectorOut)
def admin_enable_machine(
    machine_key: str,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CollectorOut:
    collector = db.get(Collector, machine_key)
    if collector is None:
        raise HTTPException(status_code=404, detail=f"Unknown machine_key: {machine_key}")
    collector.is_enabled = True
    db.commit()
    db.refresh(collector)
    return _serialize_collector(collector)


# --- Collector-facing endpoints ---


@router.post("/collector/register", response_model=CollectorOut)
def collector_register(
    payload: CollectorRegisterIn,
    request: Request,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> CollectorOut:
    if not is_valid_machine_key(payload.collector_id):
        raise HTTPException(
            status_code=422,
            detail="Invalid collector_id. Use 1-64 chars: a-z, 0-9, '-', '_', '.'",
        )
    try:
        collector = collector_hub.upsert_collector(
            db,
            collector_id=payload.collector_id,
            name=payload.name,
            display_name=payload.display_name or payload.name,
            mode=payload.mode or "collector",
            host=payload.host,
            hostname=payload.hostname,
            last_seen_ip=_client_ip(request),
            software_version=payload.software_version,
            relay_controller_mode=payload.relay_controller_mode,
            relay_controller_initialized=payload.relay_controller_initialized,
            runtime_state=payload.runtime_state,
            status_message="registered",
            touch_heartbeat=True,
        )
    except InvalidMachineKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # On registration, make sure the collector has a schedule row per relay so
    # the admin UI can immediately render per-machine schedules.
    ensure_machine_schedules(db, payload.collector_id)
    return _serialize_collector(collector)


@router.post("/collector/heartbeat", response_model=CollectorOut)
def collector_heartbeat(
    payload: CollectorHeartbeatIn,
    request: Request,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> CollectorOut:
    try:
        validate_machine_key(payload.collector_id)
    except InvalidMachineKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    collector = collector_hub.upsert_collector(
        db,
        collector_id=payload.collector_id,
        name=payload.name,
        display_name=payload.display_name,
        mode=payload.mode,
        host=payload.host,
        hostname=payload.hostname,
        last_seen_ip=_client_ip(request),
        software_version=payload.software_version,
        relay_controller_mode=payload.relay_controller_mode,
        relay_controller_initialized=payload.relay_controller_initialized,
        runtime_state=payload.runtime_state,
        status_message=payload.status_message,
    )
    # Make sure schedule rows exist for any newly-seen collector so the
    # collector's first poll always returns its own per-machine schedules.
    ensure_machine_schedules(db, payload.collector_id)
    return _serialize_collector(collector)


@router.post("/collector/sensor-readings")
def collector_ingest_sensor_readings(
    payload: CollectorSensorBatchIn,
    request: Request,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> dict:
    try:
        validate_machine_key(payload.collector_id)
    except InvalidMachineKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    collector_hub.upsert_collector(
        db, collector_id=payload.collector_id, last_seen_ip=_client_ip(request)
    )
    inserted = 0
    for reading in payload.readings:
        kwargs = dict(
            sensor_name=reading.sensor_name,
            machine_key=payload.collector_id,
            temperature=reading.temperature,
            relative_humidity=reading.relative_humidity,
            raw_payload=reading.raw_payload,
        )
        if reading.recorded_at is not None:
            ts = reading.recorded_at
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            kwargs["recorded_at"] = ts
        db.add(SensorReading(**kwargs))
        inserted += 1
    db.commit()
    return {"inserted": inserted}


@router.post("/collector/relay-events")
def collector_ingest_relay_events(
    payload: CollectorRelayBatchIn,
    request: Request,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> dict:
    try:
        validate_machine_key(payload.collector_id)
    except InvalidMachineKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    collector_hub.upsert_collector(
        db, collector_id=payload.collector_id, last_seen_ip=_client_ip(request)
    )
    inserted = 0
    for evt in payload.events:
        relay = db.get(Relay, evt.relay_id)
        if relay is None:
            continue
        db.add(
            RelayEvent(
                relay_id=evt.relay_id,
                machine_key=payload.collector_id,
                state=evt.state,
                action=evt.action,
                trigger_source=evt.trigger_source or "collector",
                success=evt.success,
                message=evt.message,
            )
        )
        inserted += 1
    # Update relay states reported by collector
    for relay_id, on in (payload.relay_states or {}).items():
        relay = db.get(Relay, relay_id)
        if relay is None:
            continue
        if relay.is_on != bool(on):
            relay.is_on = bool(on)
            from app.db.models import utcnow as _utcnow

            relay.last_changed_at = _utcnow()
    db.commit()
    return {"inserted": inserted}


@router.get("/collector/poll", response_model=CollectorPollOut)
def collector_poll(
    collector_id: str,
    request: Request,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> CollectorPollOut:
    try:
        validate_machine_key(collector_id)
    except InvalidMachineKey as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    collector_hub.upsert_collector(
        db, collector_id=collector_id, last_seen_ip=_client_ip(request)
    )
    # Make sure schedule rows exist before serving them so a fresh collector's
    # first poll always returns three schedule rows scoped to itself.
    ensure_machine_schedules(db, collector_id)
    relays = [serialize_relay(r) for r in list_relays(db)]
    schedules = list_relay_schedules(db, machine_key=collector_id)
    pending = collector_hub.fetch_pending_commands(db, collector_id)
    commands = [
        CollectorCommandOut(
            id=c.id,
            relay_id=c.relay_id,
            command_type=c.command_type,
            payload=c.payload,
            created_at=c.created_at,
        )
        for c in pending
    ]
    return CollectorPollOut(relays=relays, relay_schedules=schedules, commands=commands)


@router.post("/collector/command-ack")
def collector_command_ack(
    payload: CollectorCommandAckIn,
    _: str = Depends(require_collector_token),
    db: Session = Depends(get_db),
) -> dict:
    cmd = collector_hub.acknowledge_command(
        db,
        collector_id=payload.collector_id,
        command_id=payload.command_id,
        success=payload.success,
        message=payload.message,
    )
    if cmd is None:
        raise HTTPException(status_code=404, detail="Unknown command for collector")
    return {"id": cmd.id, "status": cmd.status}
