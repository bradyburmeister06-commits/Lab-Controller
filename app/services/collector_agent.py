"""Collector-side agent that talks to the hub over HTTP.

Runs on the lab/Windows machine. Pushes sensor readings, relay events, and
heartbeats to the hub; polls for desired schedules and commands; applies them
to local hardware via the existing relay controller / scheduler.

Stage 3 replaced the in-memory id watermark with a durable sync queue. Records
are written to local SQLite by the sensor and relay services before any upload
is attempted, and are only stamped ``synced_at`` once the hub confirms them.
Collection and relay control never wait on the network.
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
from app.db.session import SessionLocal, session_scope
from app.services import sync_queue
from app.services.relay_controller import RelayController
from app.services.relay_scheduler import RelayScheduler
from app.services.relay_service import apply_state, toggle_relay
from app.services.sync_queue import STREAM_READINGS, STREAM_RELAY_EVENTS, StreamBackoff


logger = logging.getLogger("app.collector_agent")


class SyncError(RuntimeError):
    """An upload attempt failed. The batch stays pending and is retried."""


class CollectorAgent:
    """Background loop that syncs the local lab hardware with the hub.

    Each responsibility is a separate method so it can be driven directly from
    tests without starting the thread: :meth:`register_collector`,
    :meth:`send_heartbeat`, :meth:`poll_schedules`,
    :meth:`push_pending_readings`, :meth:`push_pending_relay_events`.
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
        self._consecutive_errors = 0
        self._registered = False
        self._backoff = {
            stream: StreamBackoff(
                base=settings.collector_sync_backoff_base_seconds,
                maximum=settings.collector_sync_backoff_max_seconds,
            )
            for stream in (STREAM_READINGS, STREAM_RELAY_EVENTS)
        }
        self.last_sync_at: datetime | None = None
        self.pending_readings = 0
        self.pending_relay_events = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def collector_id(self) -> str:
        return self.settings.collector_id

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.refresh_pending_counts()
        self._thread = threading.Thread(target=self._run, daemon=True, name="collector-agent")
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown and wait briefly for the loop to reach a safe point.

        The loop only ever sleeps on ``self._stop``, so a stop during a backoff
        wait returns immediately instead of blocking for the full delay.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def status(self) -> dict[str, Any]:
        """Sync health for the health endpoint and operator troubleshooting."""
        return {
            "collector_id": self.collector_id,
            "registered": self._registered,
            "running": self.running,
            "last_sync_at": self.last_sync_at,
            "pending_readings": self.pending_readings,
            "pending_relay_events": self.pending_relay_events,
            "reading_sync_failures": self._backoff[STREAM_READINGS].failures,
            "relay_event_sync_failures": self._backoff[STREAM_RELAY_EVENTS].failures,
        }

    # --- HTTP ---

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.settings.hub_base_url,
            timeout=self.settings.collector_request_timeout_seconds,
            headers={"X-Collector-Token": self.settings.collector_api_token},
        )

    def _safe(self, message: object) -> str:
        """Never let the shared secret reach a log line."""
        return sync_queue.redact(message, self.settings.collector_api_token)

    # --- loop ---

    def _run(self) -> None:
        push_interval = max(1, int(self.settings.collector_push_interval_seconds))
        poll_interval = max(1, int(self.settings.collector_poll_interval_seconds))
        next_push = utcnow()
        next_poll = utcnow()
        while not self._stop.is_set():
            now = utcnow()
            try:
                if not self._registered:
                    self.register_collector()
                if now >= next_push:
                    self.send_heartbeat()
                    self.sync_once()
                    next_push = utcnow() + timedelta(seconds=push_interval)
                if now >= next_poll:
                    self.poll_schedules()
                    next_poll = utcnow() + timedelta(seconds=poll_interval)
                self._consecutive_errors = 0
            except Exception as exc:  # pragma: no cover - network paths
                self._consecutive_errors += 1
                backoff = sync_queue.backoff_seconds(
                    self._consecutive_errors,
                    self.settings.collector_sync_backoff_base_seconds,
                    self.settings.collector_sync_backoff_max_seconds,
                )
                logger.warning(
                    "collector loop error (#%s): %s; sleeping %ss",
                    self._consecutive_errors,
                    self._safe(exc),
                    backoff,
                )
                self._stop.wait(backoff)
                continue
            self._stop.wait(1)

    def sync_once(self) -> None:
        """Run both upload streams. Neither can prevent the other from running.

        A failing stream raises inside its own method; catching here is what
        stops one bad batch from taking down the sync service.
        """
        for push in (self.push_pending_readings, self.push_pending_relay_events):
            if self._stop.is_set():
                return
            try:
                push()
            except SyncError as exc:
                logger.warning("sync stream failed: %s", self._safe(exc))
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("unexpected sync failure: %s", self._safe(exc))

    # --- individual responsibilities ---

    def register_collector(self, client: httpx.Client | None = None) -> None:
        host = socket.gethostname()
        payload = {
            "collector_id": self.collector_id,
            "name": self.settings.collector_name,
            "display_name": self.settings.collector_name,
            "mode": self.settings.app_mode,
            "host": host,
            "hostname": host,
            "software_version": self.settings.software_version,
            "relay_controller_mode": self.settings.relay_controller,
            "relay_controller_initialized": bool(getattr(self.controller, "initialized", False)),
            "runtime_state": "starting",
        }
        self._post("/api/collector/register", payload, client=client)
        self._registered = True

    def send_heartbeat(self, client: httpx.Client | None = None) -> None:
        host = socket.gethostname()
        payload = {
            "collector_id": self.collector_id,
            "name": self.settings.collector_name,
            "display_name": self.settings.collector_name,
            "mode": self.settings.app_mode,
            "host": host,
            "hostname": host,
            "software_version": self.settings.software_version,
            "relay_controller_mode": self.settings.relay_controller,
            "relay_controller_initialized": bool(getattr(self.controller, "initialized", False)),
            "runtime_state": "running",
            "status_message": (
                f"ok; pending readings={self.pending_readings} "
                f"relay_events={self.pending_relay_events}"
            ),
        }
        self._post("/api/collector/heartbeat", payload, client=client)

    def push_pending_readings(self) -> int:
        """Upload one batch of unsynced readings. Returns records confirmed."""
        return self._push_stream(
            stream=STREAM_READINGS,
            endpoint="/api/collector/readings/batch",
            body_key="readings",
            serialize=self._serialize_reading,
        )

    def push_pending_relay_events(self) -> int:
        """Upload one batch of unsynced relay events plus current relay states."""
        return self._push_stream(
            stream=STREAM_RELAY_EVENTS,
            endpoint="/api/collector/relay-events/batch",
            body_key="events",
            serialize=self._serialize_relay_event,
            extra_body=self._relay_states,
        )

    def poll_schedules(self, client: httpx.Client | None = None) -> dict[str, Any]:
        """Pull hub-owned schedules and commands and apply them locally.

        Deliberately independent of the upload streams: relays keep following
        the hub's schedule even while the backlog cannot be shipped.
        """
        owns_client = client is None
        client = client or self._client()
        try:
            response = client.get(
                "/api/collector/poll", params={"collector_id": self.collector_id}
            )
            response.raise_for_status()
            data = response.json()
            self._apply_schedules(data.get("relay_schedules") or [])
            for cmd in data.get("commands") or []:
                if self._stop.is_set():
                    break
                ok, msg = self._apply_command(cmd)
                try:
                    client.post(
                        "/api/collector/command-ack",
                        json={
                            "collector_id": self.collector_id,
                            "command_id": cmd["id"],
                            "success": ok,
                            "message": msg,
                        },
                    ).raise_for_status()
                except Exception as exc:  # pragma: no cover
                    logger.warning(
                        "ack failed for command %s: %s", cmd.get("id"), self._safe(exc)
                    )
            return data
        finally:
            if owns_client:
                client.close()

    def refresh_pending_counts(self) -> tuple[int, int]:
        with SessionLocal() as db:
            self.pending_readings = sync_queue.pending_count(
                db, STREAM_READINGS, self.collector_id
            )
            self.pending_relay_events = sync_queue.pending_count(
                db, STREAM_RELAY_EVENTS, self.collector_id
            )
        return self.pending_readings, self.pending_relay_events

    # --- upload plumbing ---

    def _push_stream(
        self,
        *,
        stream: str,
        endpoint: str,
        body_key: str,
        serialize,
        extra_body=None,
    ) -> int:
        backoff = self._backoff[stream]
        if not backoff.ready():
            return 0

        batch_size = self.settings.collector_sync_batch_size
        with SessionLocal() as db:
            rows = sync_queue.pending_records(db, stream, self.collector_id, batch_size)
            if not rows:
                # An empty queue is healthy, not stalled. Deliberately no write
                # here: the push interval would otherwise dirty sync_state on
                # every tick of an idle collector.
                self._set_pending(stream, 0)
                backoff.on_success()
                return 0
            by_local_id = {row.local_record_id: row.id for row in rows}
            body: dict[str, Any] = {
                "collector_id": self.collector_id,
                body_key: [serialize(row) for row in rows],
            }
            if extra_body is not None:
                body.update(extra_body(db))
            row_ids = [row.id for row in rows]

        try:
            result = self._post(endpoint, body)
        except SyncError as exc:
            delay = backoff.on_failure()
            message = self._safe(exc)
            with session_scope() as tx:
                sync_queue.mark_failed(tx, stream, row_ids, message)
                sync_queue.record_failure(
                    tx, self.collector_id, stream, error=message, pending=len(row_ids)
                )
            logger.warning(
                "%s upload failed (%s records); retrying in %ss: %s",
                stream,
                len(row_ids),
                delay,
                message,
            )
            raise

        # Duplicates are records the hub already holds, so they are done. Not
        # confirming them would make a retried batch pend forever.
        confirmed_local = list(result.get("accepted") or []) + list(result.get("duplicates") or [])
        confirmed_ids = [by_local_id[lid] for lid in confirmed_local if lid in by_local_id]

        rejected = result.get("rejected") or []
        if rejected:
            # Rejects are permanent (bad range, unknown relay). Mark them synced
            # so a poison record cannot block the queue behind it forever.
            rejected_ids = [
                by_local_id[item["local_record_id"]]
                for item in rejected
                if item.get("local_record_id") in by_local_id
            ]
            logger.warning(
                "hub rejected %s %s record(s): %s",
                len(rejected),
                stream,
                self._safe(rejected[:3]),
            )
            confirmed_ids.extend(rejected_ids)

        with session_scope() as tx:
            synced = sync_queue.mark_synced(tx, stream, confirmed_ids)
            remaining = sync_queue.pending_count(tx, stream, self.collector_id)
            sync_queue.record_success(
                tx, self.collector_id, stream, synced=synced, pending=remaining
            )

        backoff.on_success()
        self.last_sync_at = utcnow()
        self._set_pending(stream, remaining)
        return synced

    def _set_pending(self, stream: str, value: int) -> None:
        if stream == STREAM_READINGS:
            self.pending_readings = value
        else:
            self.pending_relay_events = value

    def _post(
        self, endpoint: str, body: dict[str, Any], client: httpx.Client | None = None
    ) -> dict[str, Any]:
        owns_client = client is None
        client = client or self._client()
        try:
            response = client.post(endpoint, json=body)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                return {}
        except httpx.HTTPStatusError as exc:
            raise SyncError(
                f"{endpoint} returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SyncError(f"{endpoint} transport error: {exc}") from exc
        finally:
            if owns_client:
                client.close()

    def _serialize_reading(self, row: SensorReading) -> dict[str, Any]:
        return {
            "local_record_id": row.local_record_id,
            "sensor_name": row.sensor_name,
            "temperature": row.temperature,
            "relative_humidity": row.relative_humidity,
            "recorded_at": _iso(row.recorded_at),
            "raw_payload": row.raw_payload,
        }

    def _serialize_relay_event(self, row: RelayEvent) -> dict[str, Any]:
        return {
            "local_record_id": row.local_record_id,
            "relay_id": row.relay_id,
            "state": bool(row.state),
            "action": row.action,
            "trigger_source": row.trigger_source,
            "success": bool(row.success),
            "message": row.message,
            "occurred_at": _iso(row.created_at),
        }

    def _relay_states(self, db) -> dict[str, Any]:
        relays = list(db.execute(select(Relay)).scalars())
        return {"relay_states": {r.id: bool(r.is_on) for r in relays}}

    # --- applying hub state locally ---

    def _sanitize_duration(self, value: Any, fallback: int) -> int:
        """Clamp a hub-supplied cycle duration into a hardware-safe range.

        The hub is trusted but not infallible; a malformed or oversized row
        must not be able to hold a relay energised past the local cap.
        """
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = int(fallback)
        return max(1, min(seconds, self.settings.relay_max_activation_seconds))

    def _apply_schedules(self, hub_schedules: list[dict[str, Any]]) -> None:
        if not hub_schedules:
            return
        my_key = self.collector_id
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
                        on_duration_seconds=self._sanitize_duration(
                            hs.get("on_duration_seconds", 60), 60
                        ),
                        off_duration_seconds=self._sanitize_duration(
                            hs.get("off_duration_seconds", 60), 60
                        ),
                        current_phase="off",
                        next_run_at=None,
                    )
                    db.add(local)
                    changed = True
                else:
                    if bool(local.enabled) != bool(hs.get("enabled", local.enabled)):
                        local.enabled = bool(hs.get("enabled"))
                        changed = True
                    wanted_on = self._sanitize_duration(
                        hs.get("on_duration_seconds", local.on_duration_seconds),
                        local.on_duration_seconds,
                    )
                    if local.on_duration_seconds != wanted_on:
                        local.on_duration_seconds = wanted_on
                        changed = True
                    wanted_off = self._sanitize_duration(
                        hs.get("off_duration_seconds", local.off_duration_seconds),
                        local.off_duration_seconds,
                    )
                    if local.off_duration_seconds != wanted_off:
                        local.off_duration_seconds = wanted_off
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
                        machine_key=self.collector_id,
                    )
                    return True, f"set {relay_id} {'on' if on else 'off'}"
                if ctype == "relay_toggle" and relay_id:
                    toggle_relay(
                        db, relay_id, self.controller,
                        trigger_source="hub", machine_key=self.collector_id,
                    )
                    return True, f"toggled {relay_id}"
                if ctype == "schedule_changed":
                    # No-op here — schedules were already synced above.
                    return True, "schedule synced"
            return False, f"unknown command_type {ctype!r}"
        except Exception as exc:
            logger.exception("command apply failed")
            return False, self._safe(exc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()
