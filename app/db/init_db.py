from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Machine, utcnow
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
