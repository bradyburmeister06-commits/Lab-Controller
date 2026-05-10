"""End-to-end tests for the multi-collector / multi-machine architecture.

These verify that:
- The hub can host an arbitrary number of independently-registered collectors.
- Each registered collector has its own per-relay schedule rows so the three
  lab machines can run three different intervals.
- Re-registration updates an existing row instead of creating duplicates.
- Stale/online state transitions work as heartbeat age crosses the threshold.
- Public routes stay read-only and never expose admin actions.
- Invalid/unauthorized collector tokens and malformed machine keys are rejected.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import Collector, RelayEvent, RelaySchedule, SensorReading, utcnow
from app.db.session import SessionLocal
from app.main import app


ADMIN_AUTH = ("admin", "change-me-now")
TOKEN_HEADER = {"X-Collector-Token": "change-me-collector-token"}

THREE_MACHINES = [
    ("lab-collector-a", "Lab A (incubator)", 30, 90),
    ("lab-collector-b", "Lab B (humidity chamber)", 120, 600),
    ("lab-collector-c", "Lab C (oven)", 5, 5),
]


def _register(client: TestClient, machine_id: str, name: str, **extra) -> dict:
    payload = {
        "collector_id": machine_id,
        "name": name,
        "display_name": name,
        "mode": "collector",
        "host": f"{machine_id}.example",
        "hostname": f"{machine_id}-host",
        "software_version": "0.2.0",
        "relay_controller_mode": "mcc_usb1208fs_plus",
        "relay_controller_initialized": True,
    }
    payload.update(extra)
    r = client.post("/api/collector/register", headers=TOKEN_HEADER, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup_collector(machine_id: str) -> None:
    with SessionLocal() as db:
        db.query(RelaySchedule).filter(RelaySchedule.machine_key == machine_id).delete()
        db.query(SensorReading).filter(SensorReading.machine_key == machine_id).delete()
        db.query(RelayEvent).filter(RelayEvent.machine_key == machine_id).delete()
        c = db.get(Collector, machine_id)
        if c is not None:
            db.delete(c)
        db.commit()


def test_register_creates_machine_with_full_metadata():
    machine_id = "lab-collector-register-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            body = _register(client, machine_id, "Lab A", host="100.64.1.10")
            assert body["id"] == machine_id
            assert body["machine_key"] == machine_id
            assert body["display_name"] == "Lab A"
            assert body["software_version"] == "0.2.0"
            assert body["hostname"] == f"{machine_id}-host"
            assert body["online"] is True
            assert body["is_enabled"] is True

            # Re-registering must update the same row, not duplicate it.
            body2 = _register(client, machine_id, "Lab A renamed")
            assert body2["display_name"] == "Lab A renamed"

            with SessionLocal() as db:
                rows = db.query(Collector).filter(Collector.id == machine_id).all()
                assert len(rows) == 1
    finally:
        _cleanup_collector(machine_id)


def test_register_rejects_malformed_machine_key():
    bad_keys = ["", "Has Space", "UPPER", "-leading", "way-too-long-" + "x" * 80, "!!"]
    with TestClient(app) as client:
        for bad in bad_keys:
            r = client.post(
                "/api/collector/register",
                headers=TOKEN_HEADER,
                json={"collector_id": bad, "display_name": "x"},
            )
            assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_register_requires_token():
    with TestClient(app) as client:
        r = client.post(
            "/api/collector/register",
            json={"collector_id": "lab-collector-a"},
        )
        assert r.status_code == 401


def test_admin_machines_endpoint_lists_three_collectors():
    ids = [m[0] for m in THREE_MACHINES]
    for mid in ids:
        _cleanup_collector(mid)
    try:
        with TestClient(app) as client:
            for mid, name, _on, _off in THREE_MACHINES:
                _register(client, mid, name)
            r = client.get("/api/admin/machines", auth=ADMIN_AUTH)
            assert r.status_code == 200
            registered = {m["machine_key"]: m for m in r.json()}
            for mid in ids:
                assert mid in registered, registered
                assert registered[mid]["display_name"]
                assert registered[mid]["is_enabled"] is True
    finally:
        for mid in ids:
            _cleanup_collector(mid)


def test_admin_machines_endpoint_requires_auth():
    with TestClient(app) as client:
        assert client.get("/api/admin/machines").status_code == 401


def test_three_machines_have_independent_per_machine_intervals():
    """Each of the three collectors edits its own ON/OFF durations and the
    others stay untouched. The collector poll then returns ONLY that
    collector's schedules."""
    ids = [m[0] for m in THREE_MACHINES]
    for mid in ids:
        _cleanup_collector(mid)
    try:
        with TestClient(app) as client:
            for mid, name, _on, _off in THREE_MACHINES:
                _register(client, mid, name)

            # Set each machine's relay-1 schedule to its own ON/OFF durations.
            for mid, _name, on_s, off_s in THREE_MACHINES:
                r = client.patch(
                    f"/api/admin/machines/{mid}/relay-schedules/relay-1",
                    json={"enabled": False, "on_duration_seconds": on_s, "off_duration_seconds": off_s},
                    auth=ADMIN_AUTH,
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["machine_key"] == mid
                assert body["on_duration_seconds"] == on_s
                assert body["off_duration_seconds"] == off_s

            # Sanity-check: each machine's value persists independently.
            for mid, _name, on_s, off_s in THREE_MACHINES:
                r = client.get(
                    f"/api/admin/machines/{mid}/relay-schedules/relay-1",
                    auth=ADMIN_AUTH,
                )
                assert r.status_code == 200
                body = r.json()
                assert body["on_duration_seconds"] == on_s, body
                assert body["off_duration_seconds"] == off_s, body

            # Collector poll for each machine returns only its own schedules.
            for mid, _name, on_s, off_s in THREE_MACHINES:
                r = client.get(
                    "/api/collector/poll",
                    headers=TOKEN_HEADER,
                    params={"collector_id": mid},
                )
                assert r.status_code == 200, r.text
                schedules = r.json()["relay_schedules"]
                assert schedules, "expected at least one schedule row for the polled machine"
                assert {s["machine_key"] for s in schedules} == {mid}, schedules
                relay1 = next((s for s in schedules if s["relay_id"] == "relay-1"), None)
                assert relay1 is not None
                assert relay1["on_duration_seconds"] == on_s
                assert relay1["off_duration_seconds"] == off_s
    finally:
        for mid in ids:
            _cleanup_collector(mid)


def test_heartbeat_updates_last_seen_state():
    machine_id = "lab-collector-heartbeat-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            _register(client, machine_id, "HB Lab")
            r = client.post(
                "/api/collector/heartbeat",
                headers=TOKEN_HEADER,
                json={
                    "collector_id": machine_id,
                    "name": "HB Lab",
                    "mode": "collector",
                    "host": "10.0.0.5",
                    "runtime_state": "running",
                    "status_message": "ok",
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["online"] is True
            assert body["runtime_state"] == "running"
            assert body["last_status_message"] == "ok"
    finally:
        _cleanup_collector(machine_id)


def test_stale_transition_when_heartbeat_ages_past_threshold():
    machine_id = "lab-collector-stale-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            _register(client, machine_id, "Stale Lab")

            # Force a heartbeat way in the past.
            with SessionLocal() as db:
                c = db.get(Collector, machine_id)
                c.last_heartbeat_at = utcnow() - timedelta(hours=2)
                db.commit()

            r = client.get("/api/admin/machines", auth=ADMIN_AUTH)
            assert r.status_code == 200
            row = next(m for m in r.json() if m["machine_key"] == machine_id)
            assert row["online"] is False
            assert row["status"] == "stale"
            assert row["seconds_since_heartbeat"] is not None
    finally:
        _cleanup_collector(machine_id)


def test_public_dashboard_remains_read_only_and_lists_collectors():
    machine_id = "lab-collector-public-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            _register(client, machine_id, "Public Lab")
            r = client.get("/api/public/dashboard")
            assert r.status_code == 200
            body = r.json()
            assert "collectors" in body
            ids = {c["machine_key"] for c in body["collectors"]}
            assert machine_id in ids

            # Public can NOT update schedules.
            assert (
                client.patch(
                    f"/api/admin/machines/{machine_id}/relay-schedules/relay-1",
                    json={"on_duration_seconds": 1, "off_duration_seconds": 1},
                ).status_code
                == 401
            )
            # Public collectors endpoint exists and is anonymous, but only
            # exposes status — never edit hooks.
            assert client.get("/api/public/collectors").status_code == 200
    finally:
        _cleanup_collector(machine_id)


def test_admin_can_disable_machine():
    machine_id = "lab-collector-disable-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            _register(client, machine_id, "Disable Lab")
            r = client.post(
                f"/api/admin/machines/{machine_id}/disable", auth=ADMIN_AUTH
            )
            assert r.status_code == 200
            assert r.json()["is_enabled"] is False
            r = client.post(
                f"/api/admin/machines/{machine_id}/enable", auth=ADMIN_AUTH
            )
            assert r.status_code == 200
            assert r.json()["is_enabled"] is True
    finally:
        _cleanup_collector(machine_id)


def test_admin_rename_via_patch():
    machine_id = "lab-collector-rename-1"
    _cleanup_collector(machine_id)
    try:
        with TestClient(app) as client:
            _register(client, machine_id, "Rename Lab")
            r = client.patch(
                f"/api/admin/machines/{machine_id}",
                json={"display_name": "Renamed Lab"},
                auth=ADMIN_AUTH,
            )
            assert r.status_code == 200
            assert r.json()["display_name"] == "Renamed Lab"
    finally:
        _cleanup_collector(machine_id)


def test_telemetry_attributed_to_correct_machine():
    machine_a = "lab-collector-tlm-a"
    machine_b = "lab-collector-tlm-b"
    for mid in (machine_a, machine_b):
        _cleanup_collector(mid)
    try:
        with TestClient(app) as client:
            _register(client, machine_a, "A")
            _register(client, machine_b, "B")

            client.post(
                "/api/collector/sensor-readings",
                headers=TOKEN_HEADER,
                json={
                    "collector_id": machine_a,
                    "readings": [{"sensor_name": "tlm-a", "temperature": 70.0, "relative_humidity": 40.0}],
                },
            ).raise_for_status()
            client.post(
                "/api/collector/sensor-readings",
                headers=TOKEN_HEADER,
                json={
                    "collector_id": machine_b,
                    "readings": [{"sensor_name": "tlm-b", "temperature": 80.0, "relative_humidity": 50.0}],
                },
            ).raise_for_status()

            with SessionLocal() as db:
                a = db.query(SensorReading).filter(SensorReading.sensor_name == "tlm-a").all()
                b = db.query(SensorReading).filter(SensorReading.sensor_name == "tlm-b").all()
                assert all(row.machine_key == machine_a for row in a)
                assert all(row.machine_key == machine_b for row in b)
    finally:
        for mid in (machine_a, machine_b):
            _cleanup_collector(mid)


def test_command_polling_returns_only_owners_commands():
    a, b = "lab-collector-cmd-a", "lab-collector-cmd-b"
    for mid in (a, b):
        _cleanup_collector(mid)
    try:
        with TestClient(app) as client:
            _register(client, a, "A")
            _register(client, b, "B")

            # Enqueue a relay_set command for A only by toggling via admin
            # API targeted at machine_key=a.
            r = client.post(
                f"/api/relays/relay-1/on?machine_key={a}", auth=ADMIN_AUTH
            )
            assert r.status_code == 200, r.text

            r_a = client.get(
                "/api/collector/poll",
                headers=TOKEN_HEADER,
                params={"collector_id": a},
            )
            assert r_a.status_code == 200
            assert any(c["command_type"] == "relay_set" for c in r_a.json()["commands"])

            r_b = client.get(
                "/api/collector/poll",
                headers=TOKEN_HEADER,
                params={"collector_id": b},
            )
            assert r_b.status_code == 200
            assert all(c["command_type"] != "relay_set" for c in r_b.json()["commands"])
    finally:
        for mid in (a, b):
            _cleanup_collector(mid)


def test_data_summary_includes_collectors_count():
    with TestClient(app) as client:
        r = client.get("/api/data/summary", auth=ADMIN_AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "collectors" in body
        assert isinstance(body["collectors"], int)
