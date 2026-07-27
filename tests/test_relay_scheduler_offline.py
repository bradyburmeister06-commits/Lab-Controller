"""Stage 4: local scheduling reliability.

The collector must keep cycling relays from its own database while the hub or
Tailscale link is down, and must come back from a restart without replaying a
backlog of missed cycles onto the hardware.

Uses a private in-memory database so timing assertions are not affected by
whatever the shared dev DB happens to contain.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import session as session_module
from app.db.models import Relay, RelayEvent, RelaySchedule, as_utc, to_naive_utc
from app.db.session import Base
from app.services.relay_controller import MockRelayController
from app.services.relay_scheduler import DuplicateSchedulerError, RelayScheduler


BITS = {"relay-1": 0, "relay-2": 1, "relay-3": 2}
MACHINE = "collector-offline-test"


@pytest.fixture
def db_factory(monkeypatch):
    """In-memory DB, also patched over SessionLocal so tick() uses it."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        for order, relay_id in enumerate(sorted(BITS)):
            db.add(
                Relay(
                    id=relay_id,
                    name=relay_id,
                    bit_index=BITS[relay_id],
                    display_order=order,
                    enabled=True,
                )
            )
        db.commit()
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    monkeypatch.setattr("app.services.relay_scheduler.SessionLocal", factory)
    return factory


def _scheduler(max_activation: int | None = 3600) -> RelayScheduler:
    controller = MockRelayController(BITS)
    controller.initialize()
    return RelayScheduler(
        controller,
        machine_key=MACHINE,
        max_activation_seconds=max_activation,
    )


def _add_schedule(
    db,
    relay_id: str = "relay-1",
    *,
    enabled: bool = True,
    on_seconds: int = 30,
    off_seconds: int = 60,
    next_run_at: datetime | None = None,
    phase: str = "off",
    machine_key: str = MACHINE,
) -> RelaySchedule:
    sched = RelaySchedule(
        machine_key=machine_key,
        relay_id=relay_id,
        enabled=enabled,
        on_duration_seconds=on_seconds,
        off_duration_seconds=off_seconds,
        next_run_at=to_naive_utc(next_run_at) if next_run_at else None,
        current_phase=phase,
    )
    db.add(sched)
    db.commit()
    return sched


def _get(db, relay_id: str = "relay-1") -> RelaySchedule:
    return db.get(RelaySchedule, (MACHINE, relay_id))


def _events(db, relay_id: str | None = None) -> list[RelayEvent]:
    rows = list(db.execute(select(RelayEvent).order_by(RelayEvent.id)).scalars())
    return [r for r in rows if relay_id is None or r.relay_id == relay_id]


# --- offline operation ------------------------------------------------------


def test_scheduler_cycles_without_any_hub_contact(db_factory):
    """No CollectorAgent, no network: the cycle still advances."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, next_run_at=now - timedelta(seconds=1), phase="off")

        scheduler.tick()
        db.expire_all()
        assert _get(db).current_phase == "on"
        assert scheduler.controller.get_states()["relay-1"] is True

        # Force the OFF transition to be due and tick again.
        _get(db).next_run_at = to_naive_utc(now - timedelta(seconds=1))
        db.commit()
        scheduler.tick()
        db.expire_all()
        assert _get(db).current_phase == "off"
        assert scheduler.controller.get_states()["relay-1"] is False


def test_every_scheduled_activation_is_recorded_locally(db_factory):
    """Relay events are the offline audit trail the sync queue later ships."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, next_run_at=now - timedelta(seconds=1))
        scheduler.tick()
        events = _events(db, "relay-1")

    assert [e.trigger_source for e in events] == ["schedule"]
    assert events[0].state is True
    assert events[0].machine_key == MACHINE
    # Unsynced by construction — the sync queue selects on synced_at IS NULL.
    assert events[0].synced_at is None


def test_scheduler_ignores_other_machines_rows(db_factory):
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(
            db,
            next_run_at=now - timedelta(seconds=1),
            machine_key="some-other-collector",
        )
        scheduler.tick()
    assert scheduler.controller.get_states()["relay-1"] is False


# --- restart recovery / missed events ---------------------------------------


def test_load_schedules_forces_relays_off_after_a_crash_mid_cycle(db_factory):
    scheduler = _scheduler()
    scheduler.controller.turn_on("relay-1")
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, next_run_at=now + timedelta(seconds=10), phase="on")
        db.get(Relay, "relay-1").is_on = True
        db.commit()

        scheduler.load_schedules(db)
        db.expire_all()
        assert scheduler.controller.get_states()["relay-1"] is False
        assert db.get(Relay, "relay-1").is_on is False
        assert _get(db).current_phase == "off"


def test_restart_after_long_outage_skips_missed_events(db_factory):
    """A day-long outage must not fire a day's worth of cycles on boot."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(
            db,
            on_seconds=30,
            off_seconds=30,
            next_run_at=now - timedelta(days=1),
            phase="on",
        )
        scheduler.load_schedules(db)
        db.expire_all()
        sched = _get(db)

        # Exactly one catch-up transition is scheduled, starting from now.
        assert as_utc(sched.next_run_at) >= now - timedelta(seconds=5)
        assert as_utc(sched.next_run_at) <= datetime.now(timezone.utc)
        assert sched.current_phase == "off"

        recovered = [e for e in _events(db, "relay-1") if e.action == "schedule_recovered"]
        assert len(recovered) == 1
        assert "Skipped" in recovered[0].message


def test_no_recovery_event_when_nothing_was_missed(db_factory):
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, next_run_at=now + timedelta(minutes=5))
        scheduler.load_schedules(db)
        assert [e.action for e in _events(db, "relay-1")] == []


def test_load_schedules_clears_timing_for_disabled_rows(db_factory):
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, enabled=False, next_run_at=now - timedelta(hours=3), phase="on")
        scheduler.load_schedules(db)
        db.expire_all()
        assert _get(db).next_run_at is None
        assert _get(db).current_phase == "off"


def test_load_schedules_does_not_arm_a_disabled_relay(db_factory):
    """Relay.enabled is the hardware-level veto; a schedule cannot override it."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        db.get(Relay, "relay-1").enabled = False
        db.commit()
        _add_schedule(db, enabled=True, next_run_at=now - timedelta(seconds=1))
        scheduler.load_schedules(db)
        db.expire_all()
        assert _get(db).next_run_at is None


def test_tick_after_a_missed_transition_does_not_chain_fire(db_factory):
    """One overdue transition produces one flip, not one per elapsed period."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(
            db, on_seconds=5, off_seconds=5, next_run_at=now - timedelta(hours=2)
        )
        scheduler.tick()
        db.expire_all()
        sched = _get(db)
        assert sched.current_phase == "on"
        # Next flip is 5s from *now*, not 2h in the past.
        assert as_utc(sched.next_run_at) > datetime.now(timezone.utc)
        assert len(_events(db, "relay-1")) == 1


# --- duration guards --------------------------------------------------------


def test_on_duration_is_clamped_to_the_max_activation(db_factory):
    scheduler = _scheduler(max_activation=60)
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, on_seconds=9999, off_seconds=9999, next_run_at=now)
        scheduler.load_schedules(db)
        db.expire_all()
        assert _get(db).on_duration_seconds == 60
        assert _get(db).off_duration_seconds == 60


def test_validate_schedule_update_rejects_unsafe_durations():
    scheduler = _scheduler(max_activation=60)
    with pytest.raises(ValueError):
        scheduler.validate_schedule_update(on_duration_seconds=0, off_duration_seconds=None)
    with pytest.raises(ValueError):
        scheduler.validate_schedule_update(on_duration_seconds=61, off_duration_seconds=None)
    scheduler.validate_schedule_update(on_duration_seconds=60, off_duration_seconds=1)


# --- duplicate instances ----------------------------------------------------


def test_a_second_scheduler_for_the_same_machine_cannot_start(db_factory):
    first = _scheduler()
    second = _scheduler()
    try:
        first.start()
        with pytest.raises(DuplicateSchedulerError):
            second.start()
        assert second.running is False
    finally:
        second.stop()
        first.stop()


def test_the_slot_is_released_after_stop(db_factory):
    first = _scheduler()
    first.start()
    first.stop()
    second = _scheduler()
    try:
        second.start()
        assert second.running is True
    finally:
        second.stop()


def test_overlapping_advances_for_one_relay_are_serialized(db_factory):
    """_advance holds a non-blocking per-relay lock, so a re-entrant call is a no-op."""
    scheduler = _scheduler()
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        _add_schedule(db, next_run_at=now - timedelta(seconds=1))
        lock = scheduler._lock_for("relay-1")
        lock.acquire()
        try:
            scheduler._advance(db, "relay-1")
        finally:
            lock.release()
        assert scheduler.controller.get_states()["relay-1"] is False
        assert _events(db, "relay-1") == []


# --- timezone handling ------------------------------------------------------


def test_scheduler_uses_utc_and_survives_a_dst_boundary(db_factory, monkeypatch):
    """Durations are wall-clock-independent: stored timestamps are UTC.

    2025-03-09 07:00Z is the US spring-forward instant for America/Chicago. A
    schedule crossing it must advance by exactly its duration, with no lost or
    duplicated hour.
    """
    monkeypatch.setenv("TZ", "America/Chicago")
    scheduler = _scheduler()
    due = datetime(2025, 3, 9, 6, 59, 30, tzinfo=timezone.utc)
    with db_factory() as db:
        _add_schedule(db, on_seconds=60, off_seconds=60, next_run_at=due)
        scheduler._advance(db, "relay-1")
        db.expire_all()
        sched = _get(db)

    assert sched.current_phase == "on"
    assert sched.next_run_at.tzinfo is None  # stored naive-UTC by convention
    # Far in the past, so the anchor falls back to "now" rather than replaying.
    assert as_utc(sched.next_run_at) > datetime.now(timezone.utc)


def test_next_run_at_is_anchored_to_the_scheduled_time_when_on_time(db_factory):
    """Within one phase of schedule, the cycle anchors on the due time so it
    does not drift by the tick interval every cycle."""
    scheduler = _scheduler()
    due = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db_factory() as db:
        _add_schedule(db, on_seconds=60, off_seconds=60, next_run_at=due)
        scheduler._advance(db, "relay-1")
        db.expire_all()
        expected = due + timedelta(seconds=60)
        assert abs((as_utc(_get(db).next_run_at) - expected).total_seconds()) < 1
