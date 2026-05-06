"""Hub-side collector tracking and command queue helpers."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Collector, CollectorCommand, utcnow


HEARTBEAT_ONLINE_SECONDS = 60


def upsert_collector(
    db: Session,
    *,
    collector_id: str,
    name: str | None = None,
    mode: str | None = None,
    host: str | None = None,
    relay_controller_mode: str | None = None,
    relay_controller_initialized: bool | None = None,
    status_message: str | None = None,
    touch_heartbeat: bool = True,
) -> Collector:
    collector = db.get(Collector, collector_id)
    if collector is None:
        collector = Collector(id=collector_id, name=name or collector_id)
        db.add(collector)
    if name is not None:
        collector.name = name
    if mode is not None:
        collector.mode = mode
    if host is not None:
        collector.host = host
    if relay_controller_mode is not None:
        collector.relay_controller_mode = relay_controller_mode
    if relay_controller_initialized is not None:
        collector.relay_controller_initialized = bool(relay_controller_initialized)
    if status_message is not None:
        collector.last_status_message = status_message
    if touch_heartbeat:
        collector.last_heartbeat_at = utcnow()
    db.commit()
    db.refresh(collector)
    return collector


def list_collectors(db: Session) -> list[Collector]:
    return list(db.execute(select(Collector).order_by(Collector.id)).scalars())


def collector_is_online(collector: Collector, *, threshold_seconds: int = HEARTBEAT_ONLINE_SECONDS) -> bool:
    if collector.last_heartbeat_at is None:
        return False
    return (utcnow() - collector.last_heartbeat_at) <= timedelta(seconds=threshold_seconds)


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
