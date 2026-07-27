"""Collector-side sync queue over the local SQLite database.

Stage 3 moves the collector from "push whatever is newer than an in-memory id
watermark" to a durable queue. Every locally-generated record is written to
SQLite first and only marked ``synced_at`` once the hub confirms it. A collector
that loses the network keeps collecting, keeps driving relays, and ships the
backlog when the link returns.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import RelayEvent, SensorReading, SyncState, utcnow


STREAM_READINGS = "sensor_readings"
STREAM_RELAY_EVENTS = "relay_events"

_MODELS = {
    STREAM_READINGS: SensorReading,
    STREAM_RELAY_EVENTS: RelayEvent,
}

# Errors are logged verbatim apart from this. httpx puts the request URL in the
# message and our own code interpolates config, so any occurrence of the shared
# secret is masked before it can reach a log file.
_REDACTED = "***redacted***"


def redact(message: object, *secrets: str | None) -> str:
    text = str(message)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    return text


def pending_records(
    db: Session,
    stream: str,
    collector_id: str,
    limit: int,
) -> list[SensorReading | RelayEvent]:
    """Oldest-first slice of this collector's unsynced backlog.

    Ordering by primary key keeps the hub's view chronological even when the
    collector's clock jumps, and the batch cap bounds both memory and the
    request body regardless of how long the collector was offline.
    """
    model = _MODELS[stream]
    stmt = (
        select(model)
        .where(model.collector_id == collector_id, model.synced_at.is_(None))
        .order_by(model.id.asc())
        .limit(max(1, limit))
    )
    return list(db.execute(stmt).scalars())


def pending_count(db: Session, stream: str, collector_id: str) -> int:
    model = _MODELS[stream]
    return int(
        db.execute(
            select(func.count())
            .select_from(model)
            .where(model.collector_id == collector_id, model.synced_at.is_(None))
        ).scalar()
        or 0
    )


def mark_synced(db: Session, stream: str, row_ids: list[int]) -> int:
    """Confirm records the hub accepted (or already had).

    Records are never deleted here. The local copy stays as the collector's own
    history and as evidence if the hub's copy is ever questioned; pruning is a
    separate retention concern.
    """
    if not row_ids:
        return 0
    model = _MODELS[stream]
    result = db.execute(
        update(model)
        .where(model.id.in_(row_ids))
        .values(synced_at=utcnow(), last_sync_error=None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def mark_failed(db: Session, stream: str, row_ids: list[int], error: str) -> int:
    """Leave records pending but record why the attempt failed."""
    if not row_ids:
        return 0
    model = _MODELS[stream]
    result = db.execute(
        update(model)
        .where(model.id.in_(row_ids))
        .values(sync_attempts=model.sync_attempts + 1, last_sync_error=error[:500])
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def get_state(db: Session, collector_id: str, stream: str) -> SyncState:
    state = db.get(SyncState, (collector_id, stream))
    if state is None:
        state = SyncState(collector_id=collector_id, stream=stream)
        db.add(state)
        db.flush()
    return state


def record_success(
    db: Session,
    collector_id: str,
    stream: str,
    *,
    synced: int,
    pending: int,
) -> SyncState:
    state = get_state(db, collector_id, stream)
    now = utcnow()
    state.last_attempt_at = now
    state.last_success_at = now
    state.last_error = None
    state.consecutive_failures = 0
    state.synced_total = int(state.synced_total or 0) + synced
    state.pending_count = pending
    return state


def record_failure(
    db: Session,
    collector_id: str,
    stream: str,
    *,
    error: str,
    pending: int | None = None,
) -> SyncState:
    state = get_state(db, collector_id, stream)
    state.last_attempt_at = utcnow()
    state.last_error = error[:500]
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    if pending is not None:
        state.pending_count = pending
    return state


def backoff_seconds(failures: int, base: float, maximum: float) -> float:
    """Exponential backoff, capped, so a long outage does not grow unbounded."""
    if failures <= 0:
        return 0.0
    # Cap the exponent before the shift so a multi-day outage cannot overflow.
    return min(maximum, base * (2 ** min(failures - 1, 16)))


class StreamBackoff:
    """Per-stream retry gate.

    Each stream backs off on its own. A hub that rejects relay events must not
    stall sensor readings, which is the failure mode this isolates.
    """

    def __init__(self, base: float, maximum: float) -> None:
        self.base = base
        self.maximum = maximum
        self.failures = 0
        self.next_attempt_at: datetime | None = None

    def ready(self, now: datetime | None = None) -> bool:
        if self.next_attempt_at is None:
            return True
        return (now or utcnow()) >= self.next_attempt_at

    def on_success(self) -> None:
        self.failures = 0
        self.next_attempt_at = None

    def on_failure(self, now: datetime | None = None) -> float:
        self.failures += 1
        delay = backoff_seconds(self.failures, self.base, self.maximum)
        self.next_attempt_at = (now or utcnow()) + timedelta(seconds=delay)
        return delay
