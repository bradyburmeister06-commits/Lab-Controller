from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.db.models import Relay, RelaySchedule, utcnow
from app.db.session import SessionLocal
from app.main import app
from app.services.relay_controller import MockRelayController
from app.services.relay_scheduler import RelayScheduler


ADMIN_AUTH = ("admin", "change-me-now")


def _reset_schedule(relay_id: str) -> None:
    """Force a schedule row back to a known disabled-OFF state."""
    with SessionLocal() as db:
        sched = db.get(RelaySchedule, relay_id)
        if sched is None:
            sched = RelaySchedule(
                relay_id=relay_id,
                enabled=False,
                on_duration_seconds=60,
                off_duration_seconds=60,
                next_run_at=None,
                current_phase="off",
            )
            db.add(sched)
        else:
            sched.enabled = False
            sched.on_duration_seconds = 60
            sched.off_duration_seconds = 60
            sched.next_run_at = None
            sched.current_phase = "off"
        relay = db.get(Relay, relay_id)
        if relay is not None:
            relay.is_on = False
        db.commit()


def test_default_schedules_exist_for_three_relays():
    with TestClient(app) as client:
        r = client.get("/api/relay-schedules", auth=ADMIN_AUTH)
        assert r.status_code == 200
        rows = r.json()
        ids = sorted(s["relay_id"] for s in rows)
        assert ids == ["relay-1", "relay-2", "relay-3"]
        for s in rows:
            assert "enabled" in s
            assert "on_duration_seconds" in s
            assert "off_duration_seconds" in s
            assert "current_phase" in s


def test_relay_schedules_endpoint_requires_admin_auth():
    with TestClient(app) as client:
        assert client.get("/api/relay-schedules").status_code == 401
        assert client.get("/api/relays/relay-1/schedule").status_code == 401
        assert (
            client.patch("/api/relays/relay-1/schedule", json={"enabled": False}).status_code
            == 401
        )


def test_relay_schedule_unknown_relay_returns_404():
    with TestClient(app) as client:
        r = client.get("/api/relays/relay-99/schedule", auth=ADMIN_AUTH)
        assert r.status_code == 404
        r = client.patch(
            "/api/relays/relay-99/schedule",
            json={"enabled": False},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 404


def test_admin_can_update_schedule_durations():
    _reset_schedule("relay-1")
    try:
        with TestClient(app) as client:
            r = client.patch(
                "/api/relays/relay-1/schedule",
                json={"on_duration_seconds": 5, "off_duration_seconds": 7},
                auth=ADMIN_AUTH,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["on_duration_seconds"] == 5
            assert body["off_duration_seconds"] == 7
    finally:
        _reset_schedule("relay-1")


def test_schedule_invalid_duration_rejected():
    with TestClient(app) as client:
        r = client.patch(
            "/api/relays/relay-1/schedule",
            json={"on_duration_seconds": 0},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 422


def test_enable_schedule_turns_relay_on_immediately():
    _reset_schedule("relay-1")
    try:
        with TestClient(app) as client:
            r = client.patch(
                "/api/relays/relay-1/schedule",
                json={"enabled": True, "on_duration_seconds": 60, "off_duration_seconds": 60},
                auth=ADMIN_AUTH,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["enabled"] is True
            assert body["current_phase"] == "on"
            assert body["next_run_at"] is not None

            r = client.get("/api/relays/relay-1", auth=ADMIN_AUTH)
            assert r.status_code == 200
            assert r.json()["is_on"] is True
    finally:
        _reset_schedule("relay-1")


def test_disable_schedule_forces_relay_off():
    _reset_schedule("relay-2")
    try:
        with TestClient(app) as client:
            client.patch(
                "/api/relays/relay-2/schedule",
                json={"enabled": True, "on_duration_seconds": 30, "off_duration_seconds": 30},
                auth=ADMIN_AUTH,
            )
            assert client.get("/api/relays/relay-2", auth=ADMIN_AUTH).json()["is_on"] is True

            r = client.patch(
                "/api/relays/relay-2/schedule",
                json={"enabled": False},
                auth=ADMIN_AUTH,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["enabled"] is False
            assert body["current_phase"] == "off"
            assert body["next_run_at"] is None

            relay = client.get("/api/relays/relay-2", auth=ADMIN_AUTH).json()
            assert relay["is_on"] is False
    finally:
        _reset_schedule("relay-2")


def test_dashboard_includes_relay_schedules():
    with TestClient(app) as client:
        r = client.get("/api/public/dashboard")
        assert r.status_code == 200
        payload = r.json()
        assert "relay_schedules" in payload
        assert len(payload["relay_schedules"]) >= 3
        for s in payload["relay_schedules"]:
            assert "relay_id" in s
            assert "enabled" in s


def test_admin_dashboard_html_has_schedule_section():
    with TestClient(app) as client:
        r = client.get("/admin", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert "Independent schedule" in r.text
        assert "data-relay-schedule-form" in r.text


def test_scheduler_tick_advances_phase_without_real_sleep():
    """Tick the scheduler manually with an elapsed next_run_at and verify phase flips."""
    _reset_schedule("relay-3")
    try:
        controller = MockRelayController({"relay-1": 0, "relay-2": 1, "relay-3": 2})
        controller.initialize()
        scheduler = RelayScheduler(controller)
        # We call .tick() directly so we don't need to start the BackgroundScheduler.

        with SessionLocal() as db:
            sched = db.get(RelaySchedule, "relay-3")
            sched.enabled = True
            sched.on_duration_seconds = 5
            sched.off_duration_seconds = 5
            sched.current_phase = "off"
            sched.next_run_at = utcnow() - timedelta(seconds=1)
            db.commit()

        scheduler.tick()

        with SessionLocal() as db:
            sched = db.get(RelaySchedule, "relay-3")
            assert sched.current_phase == "on"
            assert sched.next_run_at is not None
            relay = db.get(Relay, "relay-3")
            assert relay.is_on is True
            sched.next_run_at = utcnow() - timedelta(seconds=1)
            db.commit()

        scheduler.tick()

        with SessionLocal() as db:
            sched = db.get(RelaySchedule, "relay-3")
            assert sched.current_phase == "off"
    finally:
        _reset_schedule("relay-3")
