"""Fail-safe, duration-bounded relay activation.

Every timed activation in the system goes through :class:`RelayActivator`. The
contract it enforces:

- A relay that was turned on is *always* turned off again, including on
  exception, on ``asyncio.CancelledError``, and on process shutdown.
- A duration must be positive and no longer than
  ``RELAY_MAX_ACTIVATION_SECONDS``.
- One relay can only have one activation in flight; an overlapping request is
  rejected rather than queued, because queuing would silently extend the total
  energised time.
- Start, end, and every failure are recorded as ``RelayEvent`` rows in the
  local database before any network call, so the audit trail survives an
  offline hub.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.db.models import Relay, utcnow
from app.db.session import SessionLocal
from app.services.relay_controller import RelayController, RelayError, safe_all_off
from app.services.relay_service import record_event


logger = logging.getLogger("app.relay_activation")


class RelayBusyError(RuntimeError):
    """The relay already has an activation in flight."""


class RelayDisabledError(RuntimeError):
    """The relay is disabled in configuration and must not be energised."""


class InvalidDurationError(ValueError):
    """The requested duration is zero, negative, or over the configured cap."""


@dataclass
class ActivationOutcome:
    relay_id: str
    requested_seconds: float
    started_at: datetime
    ended_at: datetime
    completed: bool
    cancelled: bool
    message: str

    @property
    def elapsed_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class RelayActivator:
    """Owns timed relay activations for one machine's relay controller."""

    def __init__(
        self,
        controller: RelayController,
        machine_key: str,
        max_duration_seconds: int,
        session_factory=SessionLocal,
    ) -> None:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive.")
        self.controller = controller
        self.machine_key = machine_key
        self.max_duration_seconds = max_duration_seconds
        self._session_factory = session_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, datetime] = {}

    @property
    def active_relays(self) -> dict[str, datetime]:
        return dict(self._active)

    def health(self) -> dict:
        return {
            "max_activation_seconds": self.max_duration_seconds,
            "active_activations": sorted(self._active),
            "controller": self.controller.health(),
        }

    def validate_duration(self, duration_seconds: float) -> float:
        if duration_seconds is None or duration_seconds <= 0:
            raise InvalidDurationError(
                f"Activation duration must be greater than zero, got {duration_seconds!r}."
            )
        if duration_seconds > self.max_duration_seconds:
            raise InvalidDurationError(
                f"Activation duration {duration_seconds}s exceeds the configured "
                f"maximum of {self.max_duration_seconds}s."
            )
        return float(duration_seconds)

    def _lock_for(self, relay_id: str) -> asyncio.Lock:
        lock = self._locks.get(relay_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[relay_id] = lock
        return lock

    def _record(self, **kwargs) -> None:
        """Persist an audit row. Never raises — losing the relay is worse than
        losing the log line, and this runs on the fail-safe path."""
        try:
            with self._session_factory() as db:
                record_event(db, machine_key=self.machine_key, **kwargs)
        except Exception:
            logger.exception("could not record relay event %s", kwargs.get("action"))

    def _assert_activatable(self, relay_id: str) -> None:
        if relay_id not in self.controller.bit_map:
            raise KeyError(f"Unknown relay_id: {relay_id}")
        with self._session_factory() as db:
            relay = db.get(Relay, relay_id)
            if relay is not None and not relay.enabled:
                raise RelayDisabledError(
                    f"Relay {relay_id} is disabled in configuration; refusing to activate."
                )

    async def activate(
        self,
        relay_id: str,
        duration_seconds: float,
        trigger_source: str = "api",
    ) -> ActivationOutcome:
        """Energise ``relay_id`` for ``duration_seconds``, then always de-energise.

        Raises before touching hardware for a bad duration, an unknown or
        disabled relay, or an overlapping activation.
        """
        duration = self.validate_duration(duration_seconds)
        self._assert_activatable(relay_id)

        lock = self._lock_for(relay_id)
        if lock.locked():
            raise RelayBusyError(
                f"Relay {relay_id} already has an activation in flight; refusing to overlap."
            )
        async with lock:
            return await self._run(relay_id, duration, trigger_source)

    async def _run(
        self, relay_id: str, duration: float, trigger_source: str
    ) -> ActivationOutcome:
        started_at = utcnow()
        try:
            self.controller.turn_on(relay_id)
        except RelayError as exc:
            self._record(
                relay_id=relay_id,
                state=False,
                action="activation_failed",
                success=False,
                trigger_source=trigger_source,
                message=f"turn_on failed: {exc}",
            )
            safe_all_off(self.controller, f"turn_on failure on {relay_id}")
            raise

        self._active[relay_id] = started_at
        self._record(
            relay_id=relay_id,
            state=True,
            action="activation_start",
            trigger_source=trigger_source,
            message=f"Relay {relay_id} on for {duration}s (max {self.max_duration_seconds}s).",
        )

        cancelled = False
        failure: BaseException | None = None
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised after the relay is safe
            failure = exc
            raise
        finally:
            # The one guarantee this class exists to make.
            self._active.pop(relay_id, None)
            ended_at = utcnow()
            try:
                self.controller.turn_off(relay_id)
            except RelayError as exc:
                self._record(
                    relay_id=relay_id,
                    state=True,
                    action="deactivation_failed",
                    success=False,
                    trigger_source=trigger_source,
                    message=f"turn_off failed after {duration}s: {exc}",
                )
                logger.error("relay %s failed to turn off: %s", relay_id, exc)
                safe_all_off(self.controller, f"turn_off failure on {relay_id}")
            else:
                if cancelled:
                    reason = "cancelled"
                elif failure is not None:
                    reason = f"error: {failure}"
                else:
                    reason = "completed"
                self._record(
                    relay_id=relay_id,
                    state=False,
                    action="activation_end",
                    success=failure is None,
                    trigger_source=trigger_source,
                    message=f"Relay {relay_id} off after {duration}s ({reason}).",
                )

        return ActivationOutcome(
            relay_id=relay_id,
            requested_seconds=duration,
            started_at=started_at,
            ended_at=ended_at,
            completed=True,
            cancelled=False,
            message=f"Relay {relay_id} activated for {duration}s.",
        )

    def all_off(self, reason: str = "manual") -> bool:
        """De-energise every relay and record it. Safe to call from any state."""
        ok = safe_all_off(self.controller, reason)
        self._active.clear()
        message = (
            f"all_off ({reason})" if ok
            else f"all_off ({reason}) failed; relay state is unknown."
        )
        for relay_id in sorted(self.controller.bit_map):
            self._record(
                relay_id=relay_id,
                state=False,
                action="all_off",
                success=ok,
                trigger_source=reason,
                message=message,
            )
        if ok:
            self._mark_all_relays_off()
        return ok

    def _mark_all_relays_off(self) -> None:
        try:
            with self._session_factory() as db:
                for relay_id in self.controller.bit_map:
                    relay = db.get(Relay, relay_id)
                    if relay is not None and relay.is_on:
                        relay.is_on = False
                        relay.last_changed_at = utcnow()
                db.commit()
        except Exception:
            logger.exception("could not clear Relay.is_on after all_off")
