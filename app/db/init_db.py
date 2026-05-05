from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Machine, Relay, utcnow
from app.db.session import Base, engine


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


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


def ensure_default_relays(db: Session) -> list[Relay]:
    settings = get_settings()
    defaults = [
        ("relay-1", "Relay 1", settings.relay_1_bit),
        ("relay-2", "Relay 2", settings.relay_2_bit),
        ("relay-3", "Relay 3", settings.relay_3_bit),
    ]
    relays: list[Relay] = []
    for relay_id, name, bit in defaults:
        relay = db.get(Relay, relay_id)
        if relay is None:
            relay = Relay(id=relay_id, name=name, bit_index=bit, is_on=False)
            db.add(relay)
        else:
            # Keep bit_index in sync with current configuration.
            if relay.bit_index != bit:
                relay.bit_index = bit
        relays.append(relay)
    db.commit()
    for relay in relays:
        db.refresh(relay)
    return relays
