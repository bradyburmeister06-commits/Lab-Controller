from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.db.models import (
    Relay,
    RelaySchedule,
    as_utc,
    aware_utcnow,
    to_naive_utc,
)
from app.db.session import SessionLocal
from app.services.relay_controller import RelayController, safe_all_off
from app.services.relay_service import apply_state, record_event


logger = logging.getLogger("app.relay_scheduler")

# Absolute floor on a cycle phase, so a bad row cannot spin the tick loop.
MIN_PHASE_SECONDS = 1


class DuplicateSchedulerError(RuntimeError):
    """Another scheduler for this machine_key is already running in this process."""


class RelayScheduler:
    """Per-relay independent ON/OFF cycle scheduler bound to a single machine_key.

    Each (machine_key, relay_id) pair has its own RelaySchedule row. While
    enabled, the relay cycles ON for ``on_duration_seconds`` then OFF for
    ``off_duration_seconds``, repeating. Disable safely transitions the relay
    OFF and clears next_run_at.

    The scheduler instance only ever advances schedules whose machine_key
    matches the local machine. That keeps a collector running schedule X from
    accidentally executing another collector's cycle even if both rows live in
    the same database (e.g. all_in_one development).

    Scheduling is entirely local: the hub is an optional source of schedule
    *updates*, never a dependency of schedule *execution*. A collector whose
    hub or Tailscale link is down keeps cycling from its own database.
    """

    TICK_SECONDS = 1

    # One running scheduler per machine_key per process. Two would double-fire
    # every cycle onto the same physical relay.
    _running_keys: dict[str, "RelayScheduler"] = {}
    _running_keys_guard = threading.Lock()

    def __init__(
        self,
        controller: RelayController,
        machine_key: str,
        tick_seconds: int | None = None,
        max_activation_seconds: int | None = None,
    ) -> None:
        self.controller = controller
        self.machine_key = machine_key
        self.scheduler = BackgroundScheduler(timezone=timezone.utc)
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._tick_seconds = tick_seconds or self.TICK_SECONDS
        self.max_activation_seconds = max_activation_seconds

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def start(self) -> None:
        if self.scheduler.running:
            return
        with self._running_keys_guard:
            owner = self._running_keys.get(self.machine_key)
            if owner is not None and owner is not self and owner.running:
                raise DuplicateSchedulerError(
                    f"A relay scheduler for {self.machine_key!r} is already running "
                    "in this process."
                )
            self._running_keys[self.machine_key] = self
        self.scheduler.add_job(
            self.tick,
            "interval",
            seconds=self._tick_seconds,
            id="relay-scheduler-tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        with self._running_keys_guard:
            if self._running_keys.get(self.machine_key) is self:
                del self._running_keys[self.machine_key]

    def _clamp_phase(self, seconds: int | None) -> int:
        value = max(MIN_PHASE_SECONDS, int(seconds or MIN_PHASE_SECONDS))
        if self.max_activation_seconds:
            value = min(value, int(self.max_activation_seconds))
        return value

    def _lock_for(self, relay_id: str) -> threading.Lock:
        with self._lock_guard:
            lock = self._locks.get(relay_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[relay_id] = lock
            return lock

    # --- startup ------------------------------------------------------------

    def load_schedules(self, db=None) -> list[RelaySchedule]:
        """Bring persisted schedules to a safe, consistent state at startup.

        Called before the tick loop starts. Every relay is forced OFF first —
        the process may have died mid-cycle with a relay energised — and each
        enabled schedule restarts its cycle from now. Missed events accumulated
        during the outage are deliberately *not* replayed: firing a backlog of
        duty cycles at once is the failure mode this guards against.
        """
        if db is None:
            with SessionLocal() as owned:
                return self.load_schedules(owned)

        safe_all_off(self.controller, "scheduler startup")
        now = aware_utcnow()
        loaded: list[RelaySchedule] = []
        for sched in self._schedules_for_machine(db):
            relay = db.get(Relay, sched.relay_id)
            if relay is not None and relay.is_on:
                relay.is_on = False
                relay.last_changed_at = to_naive_utc(now)
            sched.on_duration_seconds = self._clamp_phase(sched.on_duration_seconds)
            sched.off_duration_seconds = self._clamp_phase(sched.off_duration_seconds)
            if sched.enabled and (relay is None or relay.enabled):
                missed = self._missed_runs(sched, now)
                sched.current_phase = "off"
                sched.next_run_at = to_naive_utc(now)
                if missed:
                    record_event(
                        db,
                        relay_id=sched.relay_id,
                        state=False,
                        action="schedule_recovered",
                        trigger_source="startup",
                        machine_key=self.machine_key,
                        message=(
                            f"Skipped {missed} missed cycle(s) after restart; "
                            "next activation scheduled from now."
                        ),
                    )
            else:
                sched.current_phase = "off"
                sched.next_run_at = None
            db.add(sched)
            loaded.append(sched)
        db.commit()
        logger.info(
            "loaded %d relay schedule(s) for %s", len(loaded), self.machine_key
        )
        return loaded

    def _schedules_for_machine(self, db) -> list[RelaySchedule]:
        return list(
            db.execute(
                select(RelaySchedule).where(
                    RelaySchedule.machine_key == self.machine_key
                )
            ).scalars()
        )

    def _missed_runs(self, sched: RelaySchedule, now: datetime) -> int:
        """How many cycle transitions elapsed while this process was down."""
        due = as_utc(sched.next_run_at)
        if due is None or due >= now:
            return 0
        cycle = self._clamp_phase(sched.on_duration_seconds) + self._clamp_phase(
            sched.off_duration_seconds
        )
        return int((now - due).total_seconds() // max(1, cycle))

    # --- tick ---------------------------------------------------------------

    def tick(self) -> None:
        """Process schedules for this machine whose next_run_at has elapsed."""
        try:
            with SessionLocal() as db:
                now = to_naive_utc(aware_utcnow())
                schedules = list(
                    db.execute(
                        select(RelaySchedule).where(
                            RelaySchedule.machine_key == self.machine_key,
                            RelaySchedule.enabled.is_(True),
                            RelaySchedule.next_run_at.is_not(None),
                            RelaySchedule.next_run_at <= now,
                        )
                    ).scalars()
                )
                for sched in schedules:
                    self._advance(db, sched.relay_id)
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("relay scheduler tick failed")

    def _advance(self, db, relay_id: str) -> None:
        """Flip a single relay to its next phase. Holds a per-relay lock."""
        lock = self._lock_for(relay_id)
        if not lock.acquire(blocking=False):
            return
        try:
            sched = db.get(RelaySchedule, (self.machine_key, relay_id))
            if sched is None or not sched.enabled:
                return
            now = aware_utcnow()
            due = as_utc(sched.next_run_at)
            if due is None or due > now:
                return

            relay = db.get(Relay, relay_id)
            if relay is None:
                return

            next_on = sched.current_phase != "on"
            duration = self._clamp_phase(
                sched.on_duration_seconds if next_on else sched.off_duration_seconds
            )

            try:
                apply_state(
                    db,
                    relay_id,
                    next_on,
                    self.controller,
                    action="schedule",
                    trigger_source="schedule",
                    machine_key=self.machine_key,
                )
            except Exception:
                # Never leave a relay energised because bookkeeping failed.
                logger.exception("scheduled transition failed for %s", relay_id)
                safe_all_off(self.controller, f"schedule failure on {relay_id}")
                raise

            sched.current_phase = "on" if next_on else "off"
            # Anchor on the scheduled time so the cycle does not drift, but fall
            # back to "now" once we are more than one phase behind, so a long
            # outage does not replay a backlog of instant transitions.
            candidate = due + timedelta(seconds=duration)
            if candidate <= now:
                candidate = now + timedelta(seconds=duration)
            sched.next_run_at = to_naive_utc(candidate)
            db.add(sched)
            db.commit()
        finally:
            lock.release()

    # --- schedule updates ---------------------------------------------------

    def validate_schedule_update(
        self, on_duration_seconds: int | None, off_duration_seconds: int | None
    ) -> None:
        """Reject durations that would damage hardware, before they are stored."""
        for label, value in (
            ("on_duration_seconds", on_duration_seconds),
            ("off_duration_seconds", off_duration_seconds),
        ):
            if value is None:
                continue
            if int(value) < MIN_PHASE_SECONDS:
                raise ValueError(f"{label} must be at least {MIN_PHASE_SECONDS}s.")
        if (
            self.max_activation_seconds
            and on_duration_seconds is not None
            and int(on_duration_seconds) > self.max_activation_seconds
        ):
            raise ValueError(
                f"on_duration_seconds {on_duration_seconds}s exceeds the configured "
                f"maximum activation of {self.max_activation_seconds}s."
            )

    def apply_schedule_change(
        self, db, relay_id: str, machine_key: str | None = None
    ) -> RelaySchedule | None:
        """Apply schedule changes immediately without waiting for the next tick.

        ``machine_key`` defaults to the scheduler's bound machine_key. Callers
        in hub mode may pass another machine_key, but this scheduler instance
        only executes hardware changes for its own bound machine.
        """
        key = machine_key or self.machine_key
        # We only apply hardware effects when the targeted machine_key matches
        # this scheduler's local machine. Otherwise the change is recorded by
        # the caller (admin API) and shipped via collector_hub commands.
        if key != self.machine_key:
            return db.get(RelaySchedule, (key, relay_id))

        lock = self._lock_for(relay_id)
        with lock:
            sched = db.get(RelaySchedule, (key, relay_id))
            if sched is None:
                return None
            sched.on_duration_seconds = self._clamp_phase(sched.on_duration_seconds)
            sched.off_duration_seconds = self._clamp_phase(sched.off_duration_seconds)
            if not sched.enabled:
                # Safe state: force OFF and clear timing.
                relay = db.get(Relay, relay_id)
                if relay is not None and relay.is_on:
                    apply_state(
                        db,
                        relay_id,
                        False,
                        self.controller,
                        action="schedule",
                        trigger_source="schedule",
                        machine_key=key,
                    )
                sched.current_phase = "off"
                sched.next_run_at = None
                db.add(sched)
                db.commit()
                db.refresh(sched)
                return sched

            # Enabled: start the cycle now by turning ON and scheduling the OFF flip.
            apply_state(
                db,
                relay_id,
                True,
                self.controller,
                action="schedule",
                trigger_source="schedule",
                machine_key=key,
            )
            sched.current_phase = "on"
            sched.next_run_at = to_naive_utc(
                aware_utcnow() + timedelta(seconds=self._clamp_phase(sched.on_duration_seconds))
            )
            db.add(sched)
            db.commit()
            db.refresh(sched)
            return sched
