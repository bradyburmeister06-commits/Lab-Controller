"""Tests for the hub <-> collector split-mode APIs and behavior."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import Collector, CollectorCommand, RelayEvent, SensorReading, utcnow
from app.db.session import SessionLocal
from app.main import app
from app.services import collector_hub


ADMIN_AUTH = ("admin", "change-me-now")
TOKEN = "change-me-collector-token"
TOKEN_HEADER = {"X-Collector-Token": TOKEN}


def test_collector_endpoints_require_token():
    with TestClient(app) as client:
        assert client.post("/api/collector/heartbeat", json={"collector_id": "c1"}).status_code == 401
        assert client.get("/api/collector/poll?collector_id=c1").status_code == 401
        assert client.post("/api/collector/sensor-readings", json={"collector_id": "c1", "readings": []}).status_code == 401
        assert client.post("/api/collector/relay-events", json={"collector_id": "c1"}).status_code == 401
        assert client.post("/api/collector/command-ack", json={"collector_id": "c1", "command_id": 1}).status_code == 401


def test_collector_endpoints_reject_bad_token():
    with TestClient(app) as client:
        r = client.post(
            "/api/collector/heartbeat",
            headers={"X-Collector-Token": "wrong-token"},
            json={"collector_id": "c1"},
        )
        assert r.status_code == 401


def test_heartbeat_creates_and_updates_collector():
    with TestClient(app) as client:
        r = client.post(
            "/api/collector/heartbeat",
            headers=TOKEN_HEADER,
            json={
                "collector_id": "collector-test-1",
                "name": "Test Lab",
                "mode": "collector",
                "host": "lab-pc",
                "relay_controller_mode": "mcc_usb1208fs_plus",
                "relay_controller_initialized": True,
                "status_message": "ok",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "collector-test-1"
        assert body["mode"] == "collector"
        assert body["online"] is True
        assert body["relay_controller_mode"] == "mcc_usb1208fs_plus"

        # Admin can list collectors and see the new one.
        r = client.get("/api/collectors", auth=ADMIN_AUTH)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()]
        assert "collector-test-1" in ids


def test_admin_collectors_endpoint_requires_auth():
    with TestClient(app) as client:
        assert client.get("/api/collectors").status_code == 401


def test_collector_sensor_readings_ingestion():
    with TestClient(app) as client:
        r = client.post(
            "/api/collector/sensor-readings",
            headers=TOKEN_HEADER,
            json={
                "collector_id": "collector-test-2",
                "readings": [
                    {"sensor_name": "ingest-sensor", "temperature": 70.5, "relative_humidity": 41.2},
                    {"sensor_name": "ingest-sensor", "temperature": 71.0, "relative_humidity": 42.0},
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["inserted"] == 2

    with SessionLocal() as db:
        rows = db.query(SensorReading).filter(SensorReading.sensor_name == "ingest-sensor").all()
        assert len(rows) >= 2


def test_collector_relay_events_ingestion_updates_state():
    with TestClient(app) as client:
        r = client.post(
            "/api/collector/relay-events",
            headers=TOKEN_HEADER,
            json={
                "collector_id": "collector-test-3",
                "events": [
                    {"relay_id": "relay-1", "state": True, "action": "set", "trigger_source": "hub"}
                ],
                "relay_states": {"relay-1": True, "relay-2": False, "relay-3": False},
            },
        )
        assert r.status_code == 200
        assert r.json()["inserted"] == 1

        # Public dashboard now reflects the pushed state.
        relays = client.get("/api/public/relays").json()
        relay1 = next(r for r in relays if r["id"] == "relay-1")
        assert relay1["is_on"] is True

        # Reset to keep fixture clean for other tests.
        client.post(
            "/api/collector/relay-events",
            headers=TOKEN_HEADER,
            json={
                "collector_id": "collector-test-3",
                "events": [],
                "relay_states": {"relay-1": False, "relay-2": False, "relay-3": False},
            },
        ).raise_for_status()


def test_collector_poll_returns_relays_and_schedules():
    with TestClient(app) as client:
        r = client.get(
            "/api/collector/poll",
            headers=TOKEN_HEADER,
            params={"collector_id": "collector-test-4"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "relays" in body and "relay_schedules" in body and "commands" in body
        assert {r["id"] for r in body["relays"]} == {"relay-1", "relay-2", "relay-3"}


def test_collector_command_lifecycle():
    """Enqueue a command directly, poll it as the collector, then ack it."""
    with SessionLocal() as db:
        cmd = collector_hub.enqueue_command(
            db,
            collector_id="collector-test-5",
            command_type="relay_set",
            relay_id="relay-1",
            payload="on",
        )
        cmd_id = cmd.id

    with TestClient(app) as client:
        r = client.get(
            "/api/collector/poll",
            headers=TOKEN_HEADER,
            params={"collector_id": "collector-test-5"},
        )
        assert r.status_code == 200
        commands = r.json()["commands"]
        assert any(c["id"] == cmd_id and c["payload"] == "on" for c in commands)

        # Ack it.
        r = client.post(
            "/api/collector/command-ack",
            headers=TOKEN_HEADER,
            json={
                "collector_id": "collector-test-5",
                "command_id": cmd_id,
                "success": True,
                "message": "applied",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "applied"

        # A subsequent poll must NOT return the command again.
        r = client.get(
            "/api/collector/poll",
            headers=TOKEN_HEADER,
            params={"collector_id": "collector-test-5"},
        )
        assert all(c["id"] != cmd_id for c in r.json()["commands"])


def test_collector_command_ack_rejects_wrong_collector():
    with SessionLocal() as db:
        cmd = collector_hub.enqueue_command(
            db,
            collector_id="collector-test-6",
            command_type="relay_set",
            relay_id="relay-1",
            payload="off",
        )
        cmd_id = cmd.id

    with TestClient(app) as client:
        r = client.post(
            "/api/collector/command-ack",
            headers=TOKEN_HEADER,
            json={"collector_id": "different-collector", "command_id": cmd_id, "success": True},
        )
        assert r.status_code == 404


def test_collector_online_helpers():
    with SessionLocal() as db:
        c = db.get(Collector, "online-helper")
        if c is None:
            c = Collector(id="online-helper", display_name="x")
            db.add(c)
        c.last_heartbeat_at = utcnow()
        db.commit()
        assert collector_hub.collector_is_online(c) is True

        c.last_heartbeat_at = utcnow() - timedelta(minutes=10)
        db.commit()
        assert collector_hub.collector_is_online(c) is False


def test_hub_mode_admin_relay_set_enqueues_command():
    """In hub mode (no local relay controller), admin /on should enqueue a
    command for the configured collector instead of returning 503."""
    # Simulate hub mode by clearing the in-process relay controller and
    # flipping app_mode on the cached settings instance.
    import app.main as main_mod
    from app.api import routes as routes_mod  # noqa: F401 - just to ensure imported

    settings = get_settings()
    saved_mode = settings.app_mode
    saved_collector_id = settings.collector_id
    saved_module_controller = main_mod.relay_controller
    settings.app_mode = "hub"  # type: ignore[misc]
    settings.collector_id = "hub-mode-collector"  # type: ignore[misc]
    # Module-level controller is what lifespan publishes to app.state, so we
    # must clear it here for the duration of the test.
    main_mod.relay_controller = None
    try:
        # Clean any leftover queued commands for this collector id.
        with SessionLocal() as db:
            db.query(CollectorCommand).filter(
                CollectorCommand.collector_id == "hub-mode-collector"
            ).delete()
            db.commit()

        with TestClient(main_mod.app) as client:
            r = client.post("/api/relays/relay-1/on", auth=ADMIN_AUTH)
            assert r.status_code == 200, r.text
            with SessionLocal() as db:
                pending = (
                    db.query(CollectorCommand)
                    .filter(CollectorCommand.collector_id == "hub-mode-collector")
                    .all()
                )
                assert any(
                    c.command_type == "relay_set"
                    and c.relay_id == "relay-1"
                    and c.payload == "on"
                    for c in pending
                )
    finally:
        settings.app_mode = saved_mode  # type: ignore[misc]
        settings.collector_id = saved_collector_id  # type: ignore[misc]
        main_mod.relay_controller = saved_module_controller


def test_hub_mode_admin_relay_set_with_machine_key_targets_selected_collector():
    import app.main as main_mod
    settings = get_settings()
    saved_mode = settings.app_mode
    saved_collector_id = settings.collector_id
    saved_module_controller = main_mod.relay_controller
    settings.app_mode = "hub"  # type: ignore[misc]
    settings.collector_id = "hub-default-collector"  # type: ignore[misc]
    main_mod.relay_controller = None
    try:
        with SessionLocal() as db:
            db.query(CollectorCommand).filter(CollectorCommand.collector_id == "lab-mcc-controller").delete()
            db.commit()
        with TestClient(main_mod.app) as client:
            r = client.post("/api/relays/relay-1/on?machine_key=lab-mcc-controller", auth=ADMIN_AUTH)
            assert r.status_code == 200, r.text
        with SessionLocal() as db:
            pending = db.query(CollectorCommand).filter(CollectorCommand.collector_id == "lab-mcc-controller").all()
            assert any(c.command_type == "relay_set" and c.payload == "on" for c in pending)
    finally:
        settings.app_mode = saved_mode  # type: ignore[misc]
        settings.collector_id = saved_collector_id  # type: ignore[misc]
        main_mod.relay_controller = saved_module_controller


def test_hub_mode_relay_controller_info_uses_collector_status():
    """When the hub has no local controller, the controller-info endpoint
    should fall back to the last-reported collector status."""
    import app.main as main_mod

    settings = get_settings()
    saved_mode = settings.app_mode
    saved_collector_id = settings.collector_id
    saved_module_controller = main_mod.relay_controller
    settings.app_mode = "hub"  # type: ignore[misc]
    settings.collector_id = "hub-info-collector"  # type: ignore[misc]
    main_mod.relay_controller = None
    try:
        with TestClient(main_mod.app) as client:
            client.post(
                "/api/collector/heartbeat",
                headers=TOKEN_HEADER,
                json={
                    "collector_id": "hub-info-collector",
                    "name": "Lab",
                    "mode": "collector",
                    "relay_controller_mode": "mcc_usb1208fs_plus",
                    "relay_controller_initialized": True,
                },
            ).raise_for_status()
            r = client.get("/api/relays-controller", auth=ADMIN_AUTH)
            assert r.status_code == 200
            info = r.json()
            assert info["mode"] == "mcc_usb1208fs_plus"
            assert info["initialized"] is True
            r = client.get("/api/relays-controller?machine_key=hub-info-collector", auth=ADMIN_AUTH)
            assert r.status_code == 200
            info = r.json()
            assert info["digital_port"] == "REMOTE_MANAGED"
            assert info["board_num"] == -1
    finally:
        settings.app_mode = saved_mode  # type: ignore[misc]
        settings.collector_id = saved_collector_id  # type: ignore[misc]
        main_mod.relay_controller = saved_module_controller
