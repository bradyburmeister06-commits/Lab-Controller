"""Hub-side collector tracking and command queue helpers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import is_valid_machine_key
from app.db.models import Collector, CollectorCommand, RelaySchedule, utcnow


# Default fallback when settings.collector_stale_after_seconds isn't passed in.
DEFAULT_STALE_AFTER_SECONDS = 60


class InvalidMachineKey(ValueError):
    """Raised when a collector tries to register with a malformed machine_key."""


def validate_machine_key(machine_key: str | None) -> str:
    if not is_valid_machine_key(machine_key):
        raise InvalidMachineKey(
            "Invalid machine_key/collector_id. Use 1-64 chars: a-z, 0-9, '-', '_', '.' "
            "(must start with letter or digit)."
        )
    return machine_key  # type: ignore[return-value]


def upsert_collector(
    db: Session,
    *,
    collector_id: str,
    name: str | None = None,
    display_name: str | None = None,
    mode: str | None = None,
    host: str | None = None,
    hostname: str | None = None,
    last_seen_ip: str | None = None,
    software_version: str | None = None,
    relay_controller_mode: str | None = None,
    relay_controller_initialized: bool | None = None,
    runtime_state: str | None = None,
    status_message: str | None = None,
    role: str | None = None,
    is_enabled: bool | None = None,
    touch_heartbeat: bool = True,
    validate_key: bool = True,
) -> Collector:
    """Create or update a Collector record for ``collector_id``.

    Re-registration of an existing collector_id always updates the same row —
    callers never get duplicate machine entries.
    """
    if validate_key:
        validate_machine_key(collector_id)

    collector = db.get(Collector, collector_id)
    is_new = collector is None
    if is_new:
        collector = Collector(
            id=collector_id,
            display_name=display_name or name or collector_id,
            role=role or "collector",
            status="online" if touch_heartbeat else "unknown",
            is_enabled=True,
        )
        db.add(collector)
    if display_name is not None:
        collector.display_name = display_name
    elif name is not None and (is_new or not collector.display_name):
        collector.display_name = name
    if mode is not None:
        collector.mode = mode
    if host is not None:
        collector.host = host
    if hostname is not None:
        collector.hostname = hostname
    if last_seen_ip is not None:
        collector.last_seen_ip = last_seen_ip
    if software_version is not None:
        collector.software_version = software_version
    if relay_controller_mode is not None:
        collector.relay_controller_mode = relay_controller_mode
    if relay_controller_initialized is not None:
        collector.relay_controller_initialized = bool(relay_controller_initialized)
    if runtime_state is not None:
        collector.runtime_state = runtime_state
    if status_message is not None:
        collector.last_status_message = status_message
    if role is not None:
        collector.role = role
    if is_enabled is not None:
        collector.is_enabled = bool(is_enabled)
    if touch_heartbeat:
        collector.last_heartbeat_at = utcnow()
        collector.status = "online"
    db.commit()
    db.refresh(collector)
    return collector


def list_collectors(db: Session) -> list[Collector]:
    return list(db.execute(select(Collector).order_by(Collector.id)).scalars())


def collector_is_online(
    collector: Collector, *, threshold_seconds: int = DEFAULT_STALE_AFTER_SECONDS
) -> bool:
    if collector.last_heartbeat_at is None:
        return False
    return (utcnow() - collector.last_heartbeat_at) <= timedelta(seconds=threshold_seconds)


def collector_is_stale(
    collector: Collector, *, threshold_seconds: int = DEFAULT_STALE_AFTER_SECONDS
) -> bool:
    """A collector is "stale" if it has registered before but hasn't sent a
    heartbeat recently. A collector that has never sent a heartbeat is also
    treated as stale (rather than online)."""
    return not collector_is_online(collector, threshold_seconds=threshold_seconds)


def seconds_since_heartbeat(collector: Collector) -> int | None:
    if collector.last_heartbeat_at is None:
        return None
    return max(0, int((utcnow() - collector.last_heartbeat_at).total_seconds()))


def enqueue_command(
    db: Session,
    *,
    collector_id: str,
    command_type: str,
    relay_id: str | None = None,
    payload: str | None = None,
) -> CollectorCommand:
    cmd = CollectorCommand(
        collector_id=collector_id,
        command_type=command_type,
        relay_id=relay_id,
        payload=payload,
        status="pending",
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    return cmd


def fetch_pending_commands(db: Session, collector_id: str, *, limit: int = 100) -> list[CollectorCommand]:
    rows = list(
        db.execute(
            select(CollectorCommand)
            .where(
                CollectorCommand.collector_id == collector_id,
                CollectorCommand.status.in_(("pending", "delivered")),
            )
            .order_by(CollectorCommand.id.asc())
            .limit(limit)
        ).scalars()
    )
    now = utcnow()
    for cmd in rows:
        if cmd.status == "pending":
            cmd.status = "delivered"
            cmd.delivered_at = now
    if rows:
        db.commit()
        for cmd in rows:
            db.refresh(cmd)
    return rows


def acknowledge_command(
    db: Session,
    *,
    collector_id: str,
    command_id: int,
    success: bool,
    message: str | None,
) -> CollectorCommand | None:
    cmd = db.get(CollectorCommand, command_id)
    if cmd is None or cmd.collector_id != collector_id:
        return None
    cmd.status = "applied" if success else "error"
    cmd.completed_at = utcnow()
    cmd.result_message = message
    db.commit()
    db.refresh(cmd)
    return cmd


def list_schedules_for_machine(db: Session, machine_key: str) -> list[RelaySchedule]:
    return list(
        db.execute(
            select(RelaySchedule)
            .where(RelaySchedule.machine_key == machine_key)
            .order_by(RelaySchedule.relay_id)
        ).scalars()
    )


def list_all_schedules(db: Session) -> list[RelaySchedule]:
    return list(
        db.execute(
            select(RelaySchedule).order_by(RelaySchedule.machine_key, RelaySchedule.relay_id)
        ).scalars()
    )


def get_schedule(db: Session, machine_key: str, relay_id: str) -> RelaySchedule | None:
    return db.get(RelaySchedule, (machine_key, relay_id))
