"""Collector-side agent that talks to the hub over HTTP.

Runs on the lab/Windows machine. Pushes sensor readings, relay events, and
heartbeats to the hub; polls for desired schedules and commands; applies them
to local hardware via the existing relay controller / scheduler.
"""
from __future__ import annotations

import logging
import socket
import threading
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import Relay, RelayEvent, RelaySchedule, SensorReading, utcnow
from app.db.session import SessionLocal
from app.services.relay_controller import RelayController
from app.services.relay_scheduler import RelayScheduler
from app.services.relay_service import apply_state, toggle_relay


logger = logging.getLogger("app.collector_agent")


class CollectorAgent:
    """Background loop that syncs the local lab hardware with the hub.

    The agent uses the local SQLite DB as a transient buffer: sensor readings
    and relay events written by the existing services on the collector machine
    are read out, shipped to the hub, and tracked by id watermarks so we never
    duplicate uploads. Schedule/command pulls are applied to local hardware via
    the same RelayScheduler used in single-machine mode.
    """

    def __init__(
        self,
        settings: Settings,
        relay_controller: RelayController,
        relay_scheduler: RelayScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.controller = relay_controller
        self.scheduler = relay_scheduler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sensor_id = 0
        self._last_relay_event_id = 0
        self._consecutive_errors = 0
        self._registered = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._init_watermarks()
        self._thread = threading.Thread(target=self._run, daemon=True, name="collector-agent")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    # --- internal helpers ---

    def _init_watermarks(self) -> None:
        with SessionLocal() as db:
            last_s = db.execute(
                select(SensorReading.id)
                .where(SensorReading.machine_key == self.settings.collector_id)
                .order_by(SensorReading.id.desc())
                .limit(1)
            ).scalar() or db.execute(
                select(SensorReading.id).order_by(SensorReading.id.desc()).limit(1)
            ).scalar()
            last_e = db.execute(
                select(RelayEvent.id)
                .where(RelayEvent.machine_key == self.settings.collector_id)
                .order_by(RelayEvent.id.desc())
                .limit(1)
            ).scalar() or db.execute(
                select(RelayEvent.id).order_by(RelayEvent.id.desc()).limit(1)
            ).scalar()
        self._last_sensor_id = int(last_s or 0)
        self._last_relay_event_id = int(last_e or 0)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.settings.hub_base_url,
            timeout=self.settings.collector_request_timeout_seconds,
            headers={"X-Collector-Token": self.settings.collector_api_token},
        )

    def _run(self) -> None:
        push_interval = max(1, int(self.settings.collector_push_interval_seconds))
        poll_interval = max(1, int(self.settings.collector_poll_interval_seconds))
        next_push = utcnow()
        next_poll = utcnow()
        while not self._stop.is_set():
            now = utcnow()
            try:
                if not self._registered:
                    self._register_once()
                if now >= next_push:
                    self._push_once()
                    next_push = utcnow() + timedelta(seconds=push_interval)
                if now >= next_poll:
                    self._poll_once()
                    next_poll = utcnow() + timedelta(seconds=poll_interval)
                self._consecutive_errors = 0
            except Exception as exc:  # pragma: no cover - network paths
                self._consecutive_errors += 1
                backoff = min(60, 2 ** min(6, self._consecutive_errors))
                logger.warning(
                    "collector loop error (#%s): %s; sleeping %ss",
                    self._consecutive_errors,
                    exc,
                    backoff,
                )
                self._stop.wait(backoff)
                continue
            self._stop.wait(1)

    def _register_once(self) -> None:
        host = socket.gethostname()
        with self._client() as client:
            client.post(
                "/api/collector/register",
                json={
                    "collector_id": self.settings.collector_id,
                    "name": self.settings.collector_name,
                    "display_name": self.settings.collector_name,
                    "mode": self.settings.app_mode,
                    "host": host,
                    "hostname": host,
                    "software_version": self.settings.software_version,
                    "relay_controller_mode": self.settings.relay_controller,
                    "relay_controller_initialized": bool(getattr(self.controller, "_configured", True)),
                    "runtime_state": "starting",
                },
            ).raise_for_status()
        self._registered = True

    def _push_once(self) -> None:
        host = socket.gethostname()
        with self._client() as client, SessionLocal() as db:
            client.post(
                "/api/collector/heartbeat",
                json={
                    "collector_id": self.settings.collector_id,
                    "name": self.settings.collector_name,
                    "display_name": self.settings.collector_name,
                    "mode": self.settings.app_mode,
                    "host": host,
                    "hostname": host,
                    "software_version": self.settings.software_version,
                    "relay_controller_mode": self.settings.relay_controller,
                    "relay_controller_initialized": bool(getattr(self.controller, "_configured", True)),
                    "runtime_state": "running",
                    "status_message": "ok",
                },
            ).raise_for_status()

            new_readings = list(
                db.execute(
                    select(SensorReading)
                    .where(SensorReading.id > self._last_sensor_id)
                    .order_by(SensorReading.id.asc())
                    .limit(500)
                ).scalars()
            )
            if new_readings:
                client.post(
                    "/api/collector/sensor-readings",
                    json={
                        "collector_id": self.settings.collector_id,
                        "readings": [
                            {
                                "sensor_name": r.sensor_name,
                                "temperature": r.temperature,
                                "relative_humidity": r.relative_humidity,
                                "recorded_at": _iso(r.recorded_at),
                                "raw_payload": r.raw_payload,
                            }
                            for r in new_readings
                        ],
                    },
                ).raise_for_status()
                self._last_sensor_id = new_readings[-1].id

            new_events = list(
                db.execute(
                    select(RelayEvent)
                    .where(RelayEvent.id > self._last_relay_event_id)
                    .order_by(RelayEvent.id.asc())
                    .limit(500)
                ).scalars()
            )
            relays = list(db.execute(select(Relay)).scalars())
            relay_states = {r.id: bool(r.is_on) for r in relays}
            if new_events or relay_states:
                client.post(
                    "/api/collector/relay-events",
                    json={
                        "collector_id": self.settings.collector_id,
                        "events": [
                            {
                                "relay_id": e.relay_id,
                                "state": bool(e.state),
                                "action": e.action,
                                "trigger_source": e.trigger_source,
                                "success": bool(e.success),
                                "message": e.message,
                                "occurred_at": _iso(e.created_at),
                            }
                            for e in new_events
                        ],
                        "relay_states": relay_states,
                    },
                ).raise_for_status()
                if new_events:
                    self._last_relay_event_id = new_events[-1].id

    def _poll_once(self) -> None:
        with self._client() as client:
            r = client.get(
                "/api/collector/poll",
                params={"collector_id": self.settings.collector_id},
            )
            r.raise_for_status()
            data = r.json()

            # Sync schedule rows from hub into local DB so the local
            # RelayScheduler executes the hub-owned configuration. The hub
            # already filters this list to schedules scoped to our
            # collector_id, but we double-check defensively below — a
            # collector must NEVER apply another machine's intervals to its
            # own hardware.
            self._apply_schedules(data.get("relay_schedules") or [])

            for cmd in data.get("commands") or []:
                ok, msg = self._apply_command(cmd)
                try:
                    client.post(
                        "/api/collector/command-ack",
                        json={
                            "collector_id": self.settings.collector_id,
                            "command_id": cmd["id"],
                            "success": ok,
                            "message": msg,
                        },
                    ).raise_for_status()
                except Exception as exc:  # pragma: no cover
                    logger.warning("ack failed for command %s: %s", cmd.get("id"), exc)

    def _apply_schedules(self, hub_schedules: list[dict[str, Any]]) -> None:
        if not hub_schedules:
            return
        my_key = self.settings.collector_id
        with SessionLocal() as db:
            for hs in hub_schedules:
                # Defensive scoping: ignore any schedule row not scoped to us.
                schedule_key = hs.get("machine_key")
                if schedule_key is not None and schedule_key != my_key:
                    continue
                relay_id = hs.get("relay_id")
                if not relay_id:
                    continue
                if db.get(Relay, relay_id) is None:
                    continue
                local = db.get(RelaySchedule, (my_key, relay_id))
                changed = False
                if local is None:
                    local = RelaySchedule(
                        machine_key=my_key,
                        relay_id=relay_id,
                        enabled=bool(hs.get("enabled", False)),
                        on_duration_seconds=int(hs.get("on_duration_seconds", 60)),
                        off_duration_seconds=int(hs.get("off_duration_seconds", 60)),
                        current_phase="off",
                        next_run_at=None,
                    )
                    db.add(local)
                    changed = True
                else:
                    if bool(local.enabled) != bool(hs.get("enabled", local.enabled)):
                        local.enabled = bool(hs.get("enabled"))
                        changed = True
                    if local.on_duration_seconds != int(hs.get("on_duration_seconds", local.on_duration_seconds)):
                        local.on_duration_seconds = int(hs.get("on_duration_seconds"))
                        changed = True
                    if local.off_duration_seconds != int(hs.get("off_duration_seconds", local.off_duration_seconds)):
                        local.off_duration_seconds = int(hs.get("off_duration_seconds"))
                        changed = True
                if changed and self.scheduler is not None:
                    db.commit()
                    try:
                        self.scheduler.apply_schedule_change(db, relay_id, machine_key=my_key)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("apply_schedule_change failed for %s", relay_id)
            db.commit()

    def _apply_command(self, cmd: dict[str, Any]) -> tuple[bool, str | None]:
        ctype = cmd.get("command_type")
        relay_id = cmd.get("relay_id")
        payload = cmd.get("payload")
        try:
            with SessionLocal() as db:
                if ctype == "relay_set" and relay_id:
                    on = (payload == "on")
                    apply_state(
                        db, relay_id, on, self.controller,
                        action="set", trigger_source="hub",
                        machine_key=self.settings.collector_id,
                    )
                    return True, f"set {relay_id} {'on' if on else 'off'}"
                if ctype == "relay_toggle" and relay_id:
                    toggle_relay(
                        db, relay_id, self.controller,
                        trigger_source="hub", machine_key=self.settings.collector_id,
                    )
                    return True, f"toggled {relay_id}"
                if ctype == "schedule_changed":
                    # No-op here — schedules were already synced above.
                    return True, "schedule synced"
            return False, f"unknown command_type {ctype!r}"
        except Exception as exc:
            logger.exception("command apply failed")
            return False, str(exc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()
