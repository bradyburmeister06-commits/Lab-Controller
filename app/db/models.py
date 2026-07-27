from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    # Store UTC as timezone-naive values because SQLite does not preserve timezone
    # metadata reliably. Treat all database timestamps as UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def aware_utcnow() -> datetime:
    """Timezone-aware "now". Use for arithmetic and comparisons; convert with
    :func:`to_naive_utc` before storing."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Interpret a database timestamp as UTC.

    Stored values are naive-UTC by convention, so this attaches the tzinfo that
    SQLite dropped instead of assuming the process's local zone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def new_record_id() -> str:
    """Identity a locally-created row keeps for its whole life.

    The hub de-duplicates on (collector_id, local_record_id), so this value must
    be generated once at insert time and never regenerated on retry.
    """
    return uuid.uuid4().hex


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    activation_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    activations: Mapped[list["ActivationEvent"]] = relationship(back_populates="machine")


class ActivationEvent(Base):
    __tablename__ = "activation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduler")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    machine: Mapped[Machine] = relationship(back_populates="activations")


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sensor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    machine_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    relative_humidity: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    local_record_id: Mapped[str | None] = mapped_column(String(64), default=new_record_id, nullable=True)
    collector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Relay(Base):
    __tablename__ = "relays"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    bit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_on: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    events: Mapped[list["RelayEvent"]] = relationship(back_populates="relay")


class RelayEvent(Base):
    __tablename__ = "relay_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relay_id: Mapped[str] = mapped_column(ForeignKey("relays.id"), nullable=False)
    machine_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    state: Mapped[bool] = mapped_column(Boolean, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="set")
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="api")
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    local_record_id: Mapped[str | None] = mapped_column(String(64), default=new_record_id, nullable=True)
    collector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    relay: Mapped[Relay] = relationship(back_populates="events")


class RelaySchedule(Base):
    """Per-machine, per-relay independent ON/OFF cycle configuration.

    Each (machine_key, relay_id) pair owns its own enabled flag, durations,
    and current cycle state. This is what lets three collectors run three
    different intervals without colliding.
    """

    __tablename__ = "relay_schedules"

    machine_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    relay_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    on_duration_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    off_duration_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_phase: Mapped[str] = mapped_column(String(8), default="off", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Collector(Base):
    """Persistent registry of collector machines that have registered with the hub.

    This is the canonical multi-machine registry. The hub no longer relies on a
    single static machine definition in its environment file — every collector
    that registers (or sends a heartbeat) gets a row here.
    """

    __tablename__ = "collectors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="collector", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    software_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    relay_controller_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    relay_controller_initialized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    runtime_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    @property
    def name(self) -> str:
        # Back-compat alias used by older code paths and the API.
        return self.display_name


class CollectorCommand(Base):
    """Commands enqueued by the hub for a specific collector to apply to local hardware."""

    __tablename__ = "collector_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), nullable=False)
    relay_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    result_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollectorEvent(Base):
    """Operational events recorded on the collector (startup, port loss, sync failures).

    Written locally first like every other collector-generated record so an
    offline machine still keeps its own operational history.
    """

    __tablename__ = "collector_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_record_id: Mapped[str | None] = mapped_column(String(64), default=new_record_id, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncState(Base):
    """One row per (collector_id, stream) tracking sync-queue progress.

    Survives restarts so the agent can report last-successful-sync and backoff
    position without re-deriving them from the whole backlog.
    """

    __tablename__ = "sync_state"

    collector_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    synced_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


Index("ix_sensor_readings_sensor_time", SensorReading.sensor_name, SensorReading.recorded_at)
Index("ix_sensor_readings_time", SensorReading.recorded_at)
Index("ix_activation_events_machine_time", ActivationEvent.machine_id, ActivationEvent.started_at)
Index("ix_relay_events_relay_time", RelayEvent.relay_id, RelayEvent.created_at)
Index("ix_collector_commands_collector_status", CollectorCommand.collector_id, CollectorCommand.status)

# Sync-queue read path: "give me this collector's unsynced backlog, oldest first".
Index("ix_sensor_readings_unsynced", SensorReading.collector_id, SensorReading.synced_at, SensorReading.id)
Index("ix_relay_events_unsynced", RelayEvent.collector_id, RelayEvent.synced_at, RelayEvent.id)
Index("ix_relay_events_time", RelayEvent.created_at)
Index("ix_collector_events_unsynced", CollectorEvent.collector_id, CollectorEvent.synced_at, CollectorEvent.id)
Index("ix_relay_schedules_next_run", RelaySchedule.next_run_at)

# Duplicate protection. SQLite treats NULLs as distinct, so pre-Stage-3 rows
# with no local_record_id never collide with each other.
Index(
    "uq_sensor_readings_collector_local",
    SensorReading.collector_id,
    SensorReading.local_record_id,
    unique=True,
)
Index(
    "uq_relay_events_collector_local",
    RelayEvent.collector_id,
    RelayEvent.local_record_id,
    unique=True,
)
