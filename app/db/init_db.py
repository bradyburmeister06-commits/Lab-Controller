from __future__ import annotations

from datetime import timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Collector, Machine, Relay, RelaySchedule, utcnow
from app.db.session import Base, engine


_RELAY_COLUMN_MIGRATIONS: dict[str, str] = {
    "description": "TEXT",
    "enabled": "BOOLEAN NOT NULL DEFAULT 1",
    "display_order": "INTEGER NOT NULL DEFAULT 0",
}

_RELAY_EVENT_COLUMN_MIGRATIONS: dict[str, str] = {
    "machine_key": "VARCHAR(64)",
}

_SENSOR_READING_COLUMN_MIGRATIONS: dict[str, str] = {
    "machine_key": "VARCHAR(64)",
}

_COLLECTOR_COLUMN_MIGRATIONS: dict[str, str] = {
    "display_name": "VARCHAR(120)",
    "role": "VARCHAR(32) NOT NULL DEFAULT 'collector'",
    "status": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
    "is_enabled": "BOOLEAN NOT NULL DEFAULT 1",
    "hostname": "VARCHAR(255)",
    "last_seen_ip": "VARCHAR(64)",
    "software_version": "VARCHAR(64)",
    "runtime_state": "TEXT",
}


def _add_missing_columns(table: str, migrations: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    missing = [(name, ddl) for name, ddl in migrations.items() if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migrate_relay_schedules() -> None:
    """Rebuild relay_schedules with composite (machine_key, relay_id) PK if needed.

    Older databases had a single-column primary key on relay_id. We can't add a
    second primary-key column to a SQLite table in place, so we rewrite the
    table when we detect the legacy layout. Existing rows are preserved and
    assigned to the configured default machine_key.
    """
    inspector = inspect(engine)
    if "relay_schedules" not in inspector.get_table_names():
        return
    existing_cols = {col["name"] for col in inspector.get_columns("relay_schedules")}
    if "machine_key" in existing_cols:
        return

    settings = get_settings()
    default_key = settings.collector_id
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT relay_id, enabled, on_duration_seconds, off_duration_seconds,"
                " next_run_at, current_phase, created_at, updated_at FROM relay_schedules"
            )
        ).fetchall()
        conn.execute(text("DROP TABLE relay_schedules"))

    # Recreate with new schema (composite PK including machine_key).
    Base.metadata.tables["relay_schedules"].create(bind=engine)

    if not rows:
        return
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO relay_schedules"
                    " (machine_key, relay_id, enabled, on_duration_seconds,"
                    "  off_duration_seconds, next_run_at, current_phase,"
                    "  created_at, updated_at)"
                    " VALUES (:mk, :rid, :en, :on_s, :off_s, :nra, :cp, :ca, :ua)"
                ),
                {
                    "mk": default_key,
                    "rid": row[0],
                    "en": row[1],
                    "on_s": row[2],
                    "off_s": row[3],
                    "nra": row[4],
                    "cp": row[5],
                    "ca": row[6],
                    "ua": row[7],
                },
            )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _add_missing_columns("relays", _RELAY_COLUMN_MIGRATIONS)
    _add_missing_columns("relay_events", _RELAY_EVENT_COLUMN_MIGRATIONS)
    _add_missing_columns("sensor_readings", _SENSOR_READING_COLUMN_MIGRATIONS)
    _add_missing_columns("collectors", _COLLECTOR_COLUMN_MIGRATIONS)
    _migrate_relay_schedules()
    # Backfill display_name on collectors that were created with the legacy "name" column.
    inspector = inspect(engine)
    cols = {col["name"] for col in inspector.get_columns("collectors")} if "collectors" in inspector.get_table_names() else set()
    if "display_name" in cols and "name" in cols:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE collectors SET display_name = name WHERE display_name IS NULL OR display_name = ''")
            )


def ensure_default_machine(db: Session) -> Machine:
    settings = get_settings()
    machine = db.get(Machine, settings.default_machine_id)
    if machine:
        return machine

    now = utcnow()
    machine = Machine(
        id=settings.default_machine_id,
        name="Research Machine",
        enabled=True,
        interval_seconds=settings.default_interval_seconds,
        activation_duration_seconds=settings.activation_duration_seconds,
        next_run_at=now + timedelta(seconds=settings.default_interval_seconds),
    )
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


def ensure_default_relay_schedules(db: Session, machine_key: str | None = None) -> list[RelaySchedule]:
    """Ensure each existing relay has a schedule row for the given machine_key.

    In all_in_one and collector modes we seed schedules for the local machine.
    In hub mode we don't seed anything here — schedules are created the first
    time a collector registers (see ensure_machine_schedules)."""
    settings = get_settings()
    key = machine_key or settings.collector_id
    schedules: list[RelaySchedule] = []
    relays = db.query(Relay).all()
    for relay in relays:
        sched = db.get(RelaySchedule, (key, relay.id))
        if sched is None:
            sched = RelaySchedule(
                machine_key=key,
                relay_id=relay.id,
                enabled=False,
                on_duration_seconds=60,
                off_duration_seconds=60,
                next_run_at=None,
                current_phase="off",
            )
            db.add(sched)
        schedules.append(sched)
    db.commit()
    for sched in schedules:
        db.refresh(sched)
    return schedules


def ensure_machine_schedules(db: Session, machine_key: str) -> list[RelaySchedule]:
    """Seed per-relay schedule rows for a freshly registered collector.

    Skips relays that already have a schedule row for this machine_key.
    """
    schedules: list[RelaySchedule] = []
    relays = db.query(Relay).all()
    for relay in relays:
        sched = db.get(RelaySchedule, (machine_key, relay.id))
        if sched is None:
            sched = RelaySchedule(
                machine_key=machine_key,
                relay_id=relay.id,
                enabled=False,
                on_duration_seconds=60,
                off_duration_seconds=60,
                next_run_at=None,
                current_phase="off",
            )
            db.add(sched)
        schedules.append(sched)
    db.commit()
    for sched in schedules:
        db.refresh(sched)
    return schedules


def ensure_default_collector(db: Session) -> Collector | None:
    """Seed a Collector row for all_in_one mode so the local machine appears
    in the registry without registering over HTTP. Hub-only deployments stay
    empty until a real collector registers."""
    settings = get_settings()
    if settings.app_mode == "hub":
        return None
    collector = db.get(Collector, settings.collector_id)
    if collector is None:
        collector = Collector(
            id=settings.collector_id,
            display_name=settings.collector_name,
            role="collector",
            status="unknown",
            is_enabled=True,
            mode=settings.app_mode,
            relay_controller_mode=settings.relay_controller,
            software_version=settings.software_version,
        )
        db.add(collector)
        db.commit()
        db.refresh(collector)
    return collector


def ensure_default_relays(db: Session) -> list[Relay]:
    settings = get_settings()
    defaults = [
        ("relay-1", "Relay 1", settings.relay_1_bit, 1),
        ("relay-2", "Relay 2", settings.relay_2_bit, 2),
        ("relay-3", "Relay 3", settings.relay_3_bit, 3),
    ]
    relays: list[Relay] = []
    for relay_id, name, bit, order in defaults:
        relay = db.get(Relay, relay_id)
        if relay is None:
            relay = Relay(
                id=relay_id,
                name=name,
                bit_index=bit,
                is_on=False,
                enabled=True,
                display_order=order,
            )
            db.add(relay)
        else:
            # Keep bit_index in sync with current configuration.
            if relay.bit_index != bit:
                relay.bit_index = bit
            if relay.display_order in (None, 0):
                relay.display_order = order
        relays.append(relay)
    db.commit()
    for relay in relays:
        db.refresh(relay)
    return relays
