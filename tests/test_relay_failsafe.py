"""Stage 4: a relay that was turned on is always turned off again.

These tests run against their own in-memory database so they can assert on the
exact relay-event audit trail without interference from the shared dev DB.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Relay, RelayEvent
from app.db.session import Base
from app.services.relay_activation import (
    InvalidDurationError,
    RelayActivator,
    RelayBusyError,
    RelayDisabledError,
)
from app.services.relay_controller import MockRelayController, RelayConnectionError


BITS = {"relay-1": 0, "relay-2": 1, "relay-3": 2}


class FaultyController(MockRelayController):
    """Mock that can be made to fail on turn_on, turn_off, or both."""

    def __init__(self, *args, fail_on: set[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    def turn_on(self, relay_id: str) -> None:
        self.calls.append(f"on:{relay_id}")
        if "on" in self.fail_on:
            raise RelayConnectionError("simulated turn_on failure")
        super().turn_on(relay_id)

    def turn_off(self, relay_id: str) -> None:
        self.calls.append(f"off:{relay_id}")
        if "off" in self.fail_on:
            raise RelayConnectionError("simulated turn_off failure")
        super().turn_off(relay_id)

    def all_off(self) -> None:
        self.calls.append("all_off")
        if "all_off" in self.fail_on:
            raise RelayConnectionError("simulated all_off failure")
        super().all_off()


@pytest.fixture
def session_factory():
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
    return factory


def _activator(session_factory, controller=None, max_seconds=10) -> RelayActivator:
    ctrl = controller or MockRelayController(BITS)
    ctrl.initialize()
    return RelayActivator(
        ctrl,
        machine_key="collector-test",
        max_duration_seconds=max_seconds,
        session_factory=session_factory,
    )


def _actions(session_factory, relay_id: str | None = None) -> list[str]:
    with session_factory() as db:
        stmt = select(RelayEvent).order_by(RelayEvent.id)
        rows = list(db.execute(stmt).scalars())
    return [r.action for r in rows if relay_id is None or r.relay_id == relay_id]


# --- duration validation ----------------------------------------------------


@pytest.mark.parametrize("duration", [0, -1, -0.5])
def test_zero_or_negative_duration_is_rejected(session_factory, duration):
    activator = _activator(session_factory)
    with pytest.raises(InvalidDurationError):
        asyncio.run(activator.activate("relay-1", duration))
    assert activator.controller.get_states()["relay-1"] is False
    assert _actions(session_factory) == []


def test_duration_over_the_maximum_is_rejected(session_factory):
    activator = _activator(session_factory, max_seconds=5)
    with pytest.raises(InvalidDurationError) as excinfo:
        asyncio.run(activator.activate("relay-1", 6))
    assert "maximum" in str(excinfo.value)
    assert activator.controller.get_states()["relay-1"] is False


def test_unknown_relay_is_rejected_before_any_write(session_factory):
    activator = _activator(session_factory)
    with pytest.raises(KeyError):
        asyncio.run(activator.activate("relay-99", 0.01))


def test_disabled_relay_is_never_energised(session_factory):
    with session_factory() as db:
        db.get(Relay, "relay-2").enabled = False
        db.commit()
    activator = _activator(session_factory)
    with pytest.raises(RelayDisabledError):
        asyncio.run(activator.activate("relay-2", 0.01))
    assert activator.controller.get_states()["relay-2"] is False


# --- the core guarantee -----------------------------------------------------


def test_activation_turns_the_relay_off_again(session_factory):
    activator = _activator(session_factory)
    outcome = asyncio.run(activator.activate("relay-1", 0.01))
    assert outcome.completed is True
    assert outcome.requested_seconds == 0.01
    assert activator.controller.get_states()["relay-1"] is False
    assert _actions(session_factory, "relay-1") == ["activation_start", "activation_end"]


def test_exception_during_activation_still_turns_the_relay_off(session_factory):
    """The sleep is patched to blow up mid-activation; finally must still fire."""
    activator = _activator(session_factory)

    async def boom(_seconds):
        raise RuntimeError("something broke mid-activation")

    async def run():
        original = asyncio.sleep
        asyncio.sleep = boom  # type: ignore[assignment]
        try:
            await activator.activate("relay-1", 1)
        finally:
            asyncio.sleep = original  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        asyncio.run(run())

    assert activator.controller.get_states()["relay-1"] is False
    actions = _actions(session_factory, "relay-1")
    assert actions == ["activation_start", "activation_end"]
    assert activator.active_relays == {}


def test_cancellation_turns_the_relay_off(session_factory):
    activator = _activator(session_factory)

    async def run():
        task = asyncio.create_task(activator.activate("relay-1", 10))
        # Let the activation reach its sleep before cancelling.
        await asyncio.sleep(0.05)
        assert activator.controller.get_states()["relay-1"] is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert activator.controller.get_states()["relay-1"] is False
    assert _actions(session_factory, "relay-1") == ["activation_start", "activation_end"]


def test_turn_on_failure_records_and_triggers_emergency_all_off(session_factory):
    controller = FaultyController(BITS, fail_on={"on"})
    activator = _activator(session_factory, controller)
    with pytest.raises(RelayConnectionError):
        asyncio.run(activator.activate("relay-1", 0.01))
    assert "all_off" in controller.calls
    assert _actions(session_factory, "relay-1") == ["activation_failed"]


def test_turn_off_failure_is_recorded_and_escalates_to_all_off(session_factory):
    controller = FaultyController(BITS, fail_on={"off"})
    activator = _activator(session_factory, controller)
    controller.calls.clear()  # drop the all_off from initialize()
    asyncio.run(activator.activate("relay-1", 0.01))
    assert _actions(session_factory, "relay-1") == [
        "activation_start",
        "deactivation_failed",
    ]
    assert controller.calls == ["on:relay-1", "off:relay-1", "all_off"]


# --- overlap protection -----------------------------------------------------


def test_overlapping_activation_of_the_same_relay_is_rejected(session_factory):
    activator = _activator(session_factory)

    async def run():
        first = asyncio.create_task(activator.activate("relay-1", 1))
        await asyncio.sleep(0.05)
        with pytest.raises(RelayBusyError):
            await activator.activate("relay-1", 0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(run())
    assert activator.controller.get_states()["relay-1"] is False


def test_different_relays_may_activate_concurrently(session_factory):
    activator = _activator(session_factory)

    async def run():
        await asyncio.gather(
            activator.activate("relay-1", 0.05),
            activator.activate("relay-2", 0.05),
        )

    asyncio.run(run())
    assert activator.controller.get_states() == {
        "relay-1": False,
        "relay-2": False,
        "relay-3": False,
    }


def test_relay_is_reported_active_while_energised(session_factory):
    activator = _activator(session_factory)

    async def run():
        task = asyncio.create_task(activator.activate("relay-3", 1))
        await asyncio.sleep(0.05)
        assert "relay-3" in activator.active_relays
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert activator.active_relays == {}


# --- manual all-off ---------------------------------------------------------


def test_all_off_clears_hardware_database_and_audit_trail(session_factory):
    activator = _activator(session_factory)
    activator.controller.turn_on("relay-1")
    with session_factory() as db:
        db.get(Relay, "relay-1").is_on = True
        db.commit()

    assert activator.all_off("api") is True
    assert activator.controller.get_states()["relay-1"] is False
    with session_factory() as db:
        assert db.get(Relay, "relay-1").is_on is False
    assert _actions(session_factory) == ["all_off", "all_off", "all_off"]


def test_all_off_failure_is_reported_not_raised(session_factory):
    controller = FaultyController(BITS)
    activator = _activator(session_factory, controller)
    controller.fail_on = {"all_off"}
    assert activator.all_off("api") is False
    with session_factory() as db:
        events = list(db.execute(select(RelayEvent)).scalars())
    assert events and all(e.success is False for e in events)


def test_health_reports_cap_and_controller_state(session_factory):
    activator = _activator(session_factory, max_seconds=42)
    health = activator.health()
    assert health["max_activation_seconds"] == 42
    assert health["active_activations"] == []
    assert health["controller"]["initialized"] is True
