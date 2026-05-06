from __future__ import annotations

from datetime import timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Machine, Relay, RelaySchedule, utcnow
from app.db.session import Base, engine


_RELAY_COLUMN_MIGRATIONS: dict[str, str] = {
    "description": "TEXT",
    "enabled": "BOOLEAN NOT NULL DEFAULT 1",
    "display_order": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_relay_columns() -> None:
    """Add new optional columns to the relays table on existing SQLite databases."""

    inspector = inspect(engine)
    if "relays" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("relays")}
    missing = [(name, ddl) for name, ddl in _RELAY_COLUMN_MIGRATIONS.items() if name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE relays ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_relay_columns()


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


def ensure_default_relay_schedules(db: Session) -> list[RelaySchedule]:
    """Ensure each existing relay has a schedule row. New schedules default to disabled."""
    schedules: list[RelaySchedule] = []
    relays = db.query(Relay).all()
    for relay in relays:
        sched = db.get(RelaySchedule, relay.id)
        if sched is None:
            sched = RelaySchedule(
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
