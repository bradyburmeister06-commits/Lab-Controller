from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Relay, RelayEvent, utcnow
from app.services.relay_controller import RelayController


DEFAULT_RELAY_IDS = ("relay-1", "relay-2", "relay-3")


def list_relays(db: Session) -> list[Relay]:
    return list(
        db.execute(select(Relay).order_by(Relay.display_order, Relay.id)).scalars()
    )


def get_relay(db: Session, relay_id: str) -> Relay:
    relay = db.get(Relay, relay_id)
    if not relay:
        raise ValueError(f"Unknown relay_id: {relay_id}")
    return relay


def relay_history(db: Session, relay_id: str | None = None, limit: int = 200) -> list[RelayEvent]:
    stmt = select(RelayEvent).order_by(desc(RelayEvent.created_at)).limit(limit)
    if relay_id is not None:
        stmt = (
            select(RelayEvent)
            .where(RelayEvent.relay_id == relay_id)
            .order_by(desc(RelayEvent.created_at))
            .limit(limit)
        )
    return list(db.execute(stmt).scalars())


def apply_state(
    db: Session,
    relay_id: str,
    on: bool,
    controller: RelayController,
    action: str = "set",
    trigger_source: str = "api",
) -> tuple[Relay, RelayEvent]:
    relay = get_relay(db, relay_id)
    if on and not relay.enabled:
        event = RelayEvent(
            relay_id=relay.id,
            state=relay.is_on,
            action=action,
            trigger_source=trigger_source,
            success=False,
            message=f"Relay {relay.id} is disabled in configuration; ignoring on command.",
        )
        db.add(event)
        db.commit()
        db.refresh(relay)
        db.refresh(event)
        return relay, event
    result = controller.set_state(relay_id, on)
    if result.success:
        relay.is_on = on
        relay.last_changed_at = utcnow()
    event = RelayEvent(
        relay_id=relay.id,
        state=on,
        action=action,
        trigger_source=trigger_source,
        success=result.success,
        message=result.message,
    )
    db.add(event)
    db.commit()
    db.refresh(relay)
    db.refresh(event)
    return relay, event


def toggle_relay(
    db: Session,
    relay_id: str,
    controller: RelayController,
    trigger_source: str = "api",
) -> tuple[Relay, RelayEvent]:
    relay = get_relay(db, relay_id)
    return apply_state(
        db,
        relay_id,
        not relay.is_on,
        controller,
        action="toggle",
        trigger_source=trigger_source,
    )
