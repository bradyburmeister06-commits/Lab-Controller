from __future__ import annotations

import logging
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.db.models import Relay, RelaySchedule, utcnow
from app.db.session import SessionLocal
from app.services.relay_controller import RelayController
from app.services.relay_service import apply_state


logger = logging.getLogger("app.relay_scheduler")


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
    """

    TICK_SECONDS = 1

    def __init__(
        self,
        controller: RelayController,
        machine_key: str,
        tick_seconds: int | None = None,
    ) -> None:
        self.controller = controller
        self.machine_key = machine_key
        self.scheduler = BackgroundScheduler()
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._tick_seconds = tick_seconds or self.TICK_SECONDS

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.add_job(
            self.tick,
            "interval",
            seconds=self._tick_seconds,
            id="relay-scheduler-tick",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _lock_for(self, relay_id: str) -> threading.Lock:
        with self._lock_guard:
            lock = self._locks.get(relay_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[relay_id] = lock
            return lock

    def tick(self) -> None:
        """Process schedules for this machine whose next_run_at has elapsed."""
        try:
            with SessionLocal() as db:
                now = utcnow()
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
            now = utcnow()
            if sched.next_run_at is None or sched.next_run_at > now:
                return

            relay = db.get(Relay, relay_id)
            if relay is None:
                return

            # Determine next phase. We flip from current phase.
            next_on = sched.current_phase != "on"
            duration = sched.on_duration_seconds if next_on else sched.off_duration_seconds

            apply_state(
                db,
                relay_id,
                next_on,
                self.controller,
                action="schedule",
                trigger_source="schedule",
                machine_key=self.machine_key,
            )
            sched.current_phase = "on" if next_on else "off"
            sched.next_run_at = now + timedelta(seconds=max(1, int(duration)))
            db.add(sched)
            db.commit()
        finally:
            lock.release()

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
            sched.next_run_at = utcnow() + timedelta(seconds=max(1, int(sched.on_duration_seconds)))
            db.add(sched)
            db.commit()
            db.refresh(sched)
            return sched
