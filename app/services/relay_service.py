from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Relay, RelayEvent, utcnow
from app.services.relay_controller import RelayController


DEFAULT_RELAY_IDS = ("relay-1", "relay-2", "relay-3")


def _commit(db: Session) -> None:
    """Commit, rolling back so a failed write never leaves the session dirty for
    the next relay operation on the same connection."""
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def list_relays(db: Session) -> list[Relay]:
    return list(
        db.execute(select(Relay).order_by(Relay.display_order, Relay.id)).scalars()
    )


def get_relay(db: Session, relay_id: str) -> Relay:
    relay = db.get(Relay, relay_id)
    if not relay:
        raise ValueError(f"Unknown relay_id: {relay_id}")
    return relay


def relay_history(
    db: Session,
    relay_id: str | None = None,
    limit: int = 200,
    machine_key: str | None = None,
) -> list[RelayEvent]:
    stmt = select(RelayEvent).order_by(desc(RelayEvent.created_at)).limit(limit)
    if relay_id is not None and machine_key is not None:
        stmt = (
            select(RelayEvent)
            .where(RelayEvent.relay_id == relay_id, RelayEvent.machine_key == machine_key)
            .order_by(desc(RelayEvent.created_at))
            .limit(limit)
        )
    elif relay_id is not None:
        stmt = (
            select(RelayEvent)
            .where(RelayEvent.relay_id == relay_id)
            .order_by(desc(RelayEvent.created_at))
            .limit(limit)
        )
    elif machine_key is not None:
        stmt = (
            select(RelayEvent)
            .where(RelayEvent.machine_key == machine_key)
            .order_by(desc(RelayEvent.created_at))
            .limit(limit)
        )
    return list(db.execute(stmt).scalars())


def record_event(
    db: Session,
    relay_id: str,
    state: bool,
    action: str,
    message: str,
    success: bool = True,
    trigger_source: str = "api",
    machine_key: str | None = None,
) -> RelayEvent:
    """Append a relay event without touching hardware.

    Used by the fail-safe activation path to record activation start/end and
    the failures around them. Written locally regardless of hub reachability;
    the sync queue picks it up from ``synced_at IS NULL``.
    """
    event = RelayEvent(
        relay_id=relay_id,
        machine_key=machine_key,
        collector_id=machine_key,
        state=state,
        action=action,
        trigger_source=trigger_source,
        success=success,
        message=message,
    )
    db.add(event)
    _commit(db)
    db.refresh(event)
    return event


def apply_state(
    db: Session,
    relay_id: str,
    on: bool,
    controller: RelayController,
    action: str = "set",
    trigger_source: str = "api",
    machine_key: str | None = None,
) -> tuple[Relay, RelayEvent]:
    relay = get_relay(db, relay_id)
    if on and not relay.enabled:
        event = RelayEvent(
            relay_id=relay.id,
            machine_key=machine_key,
            collector_id=machine_key,
            state=relay.is_on,
            action=action,
            trigger_source=trigger_source,
            success=False,
            message=f"Relay {relay.id} is disabled in configuration; ignoring on command.",
        )
        db.add(event)
        _commit(db)
        db.refresh(relay)
        db.refresh(event)
        return relay, event
    result = controller.set_state(relay_id, on)
    if result.success:
        relay.is_on = on
        relay.last_changed_at = utcnow()
    # Recorded locally whether or not the hub is reachable; the sync queue picks
    # it up later from synced_at IS NULL.
    event = RelayEvent(
        relay_id=relay.id,
        machine_key=machine_key,
        collector_id=machine_key,
        state=on,
        action=action,
        trigger_source=trigger_source,
        success=result.success,
        message=result.message,
    )
    db.add(event)
    _commit(db)
    db.refresh(relay)
    db.refresh(event)
    return relay, event


def toggle_relay(
    db: Session,
    relay_id: str,
    controller: RelayController,
    trigger_source: str = "api",
    machine_key: str | None = None,
) -> tuple[Relay, RelayEvent]:
    relay = get_relay(db, relay_id)
    return apply_state(
        db,
        relay_id,
        not relay.is_on,
        controller,
        action="toggle",
        trigger_source=trigger_source,
        machine_key=machine_key,
    )
