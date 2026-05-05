from __future__ import annotations

from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import ActivationEvent, Machine, utcnow
from app.services.machine_controller import MachineController


def get_machine(db: Session, machine_id: str) -> Machine:
    machine = db.get(Machine, machine_id)
    if not machine:
        raise ValueError(f"Unknown machine_id: {machine_id}")
    return machine


def get_last_activation(db: Session, machine_id: str) -> ActivationEvent | None:
    return db.execute(
        select(ActivationEvent)
        .where(ActivationEvent.machine_id == machine_id)
        .order_by(desc(ActivationEvent.started_at))
        .limit(1)
    ).scalar_one_or_none()


def trigger_machine(
    db: Session,
    machine_id: str,
    controller: MachineController,
    trigger_source: str = "manual",
) -> ActivationEvent:
    machine = get_machine(db, machine_id)
    started = utcnow()
    event = ActivationEvent(machine_id=machine.id, started_at=started, status="started", trigger_source=trigger_source)
    db.add(event)
    db.commit()
    db.refresh(event)

    result = controller.turn_on(machine.id)
    event.completed_at = utcnow()
    event.status = "success" if result.success else "failed"
    event.message = result.message
    machine.next_run_at = event.completed_at + timedelta(seconds=machine.interval_seconds)
    db.commit()
    db.refresh(event)
    return event


def reschedule_machine(db: Session, machine: Machine) -> Machine:
    machine.next_run_at = utcnow() + timedelta(seconds=machine.interval_seconds)
    db.commit()
    db.refresh(machine)
    return machine


def seconds_until(dt) -> int | None:
    if dt is None:
        return None
    delta = dt - utcnow()
    return max(0, int(delta.total_seconds()))
