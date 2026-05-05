from fastapi.testclient import TestClient

from app.main import app
from app.services.relay_controller import (
    MockRelayController,
    build_relay_controller,
)
from app.config import Settings


ADMIN_AUTH = ("admin", "change-me-now")


def test_mock_controller_masks_bits_independently():
    ctrl = MockRelayController({"relay-1": 0, "relay-2": 1, "relay-3": 2}, active_high=True)
    ctrl.initialize()
    assert ctrl.latch == 0
    assert ctrl.set_state("relay-1", True).success is True
    assert ctrl.latch == 0b001
    assert ctrl.set_state("relay-3", True).success is True
    assert ctrl.latch == 0b101
    assert ctrl.set_state("relay-1", False).success is True
    assert ctrl.latch == 0b100
    assert ctrl.get_state("relay-3") is True
    assert ctrl.get_state("relay-1") is False


def test_mock_controller_active_low_inverts_levels():
    ctrl = MockRelayController({"relay-1": 0}, active_high=False)
    # Default latch starts at 0; with active_low this means "on" pin high == off relay.
    assert ctrl.set_state("relay-1", True).success is True
    # active_low: on => bit cleared
    assert ctrl.latch & 0b1 == 0
    assert ctrl.get_state("relay-1") is True
    ctrl.set_state("relay-1", False)
    assert ctrl.latch & 0b1 == 1
    assert ctrl.get_state("relay-1") is False


def test_mock_controller_unknown_relay_returns_failure():
    ctrl = MockRelayController({"relay-1": 0}, active_high=True)
    result = ctrl.set_state("relay-x", True)
    assert result.success is False
    assert "Unknown" in result.message


def test_build_relay_controller_defaults_to_mock():
    settings = Settings()
    ctrl = build_relay_controller(settings)
    assert isinstance(ctrl, MockRelayController)


def test_public_relays_endpoint_returns_three_defaults():
    with TestClient(app) as client:
        response = client.get("/api/public/relays")
        assert response.status_code == 200
        relays = response.json()
        ids = sorted(r["id"] for r in relays)
        assert ids == ["relay-1", "relay-2", "relay-3"]
        for r in relays:
            assert r["is_on"] is False or r["is_on"] is True
            assert "bit_index" in r


def test_admin_relay_controls_set_on_off_toggle_and_history():
    with TestClient(app) as client:
        # Auth required
        assert client.post("/api/relays/relay-1/on").status_code == 401

        # Turn relay-1 on
        r = client.post("/api/relays/relay-1/on", auth=ADMIN_AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["is_on"] is True

        # Turn relay-1 off
        r = client.post("/api/relays/relay-1/off", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_on"] is False

        # Toggle should flip back to on
        r = client.post("/api/relays/relay-1/toggle", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_on"] is True

        # Explicit set with JSON body
        r = client.post("/api/relays/relay-1/set", json={"on": False}, auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_on"] is False

        # Get one
        r = client.get("/api/relays/relay-1", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["id"] == "relay-1"

        # History endpoints
        r = client.get("/api/relays/relay-1/events?limit=10", auth=ADMIN_AUTH)
        assert r.status_code == 200
        events = r.json()
        assert len(events) >= 4
        # Newest event first
        actions = [e["action"] for e in events]
        assert "set" in actions or "toggle" in actions

        r = client.get("/api/relay-events?limit=10", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_admin_relay_unknown_id_returns_404():
    with TestClient(app) as client:
        r = client.post("/api/relays/relay-99/on", auth=ADMIN_AUTH)
        assert r.status_code == 404


def test_dashboard_includes_relays():
    with TestClient(app) as client:
        r = client.get("/api/public/dashboard")
        assert r.status_code == 200
        payload = r.json()
        assert "relays" in payload
        assert len(payload["relays"]) == 3
        for relay in payload["relays"]:
            # New metadata fields must be present in the public payload too.
            assert "enabled" in relay
            assert "display_order" in relay


def test_admin_relay_metadata_can_be_edited():
    with TestClient(app) as client:
        # Anonymous PATCH must be rejected.
        assert client.patch("/api/relays/relay-1", json={"name": "x"}).status_code == 401

        payload = {
            "name": "Vacuum pump",
            "description": "PB.0 opto-isolated module",
            "enabled": True,
            "display_order": 5,
        }
        r = client.patch("/api/relays/relay-1", json=payload, auth=ADMIN_AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Vacuum pump"
        assert body["description"] == "PB.0 opto-isolated module"
        assert body["enabled"] is True
        assert body["display_order"] == 5

        # The admin list endpoint should reflect the edit.
        r = client.get("/api/relays", auth=ADMIN_AUTH)
        assert r.status_code == 200
        relays = {row["id"]: row for row in r.json()}
        assert relays["relay-1"]["name"] == "Vacuum pump"
        assert relays["relay-1"]["description"] == "PB.0 opto-isolated module"


def test_admin_relay_metadata_unknown_id_returns_404():
    with TestClient(app) as client:
        r = client.patch("/api/relays/relay-99", json={"name": "no"}, auth=ADMIN_AUTH)
        assert r.status_code == 404


def test_disabled_relay_refuses_on_but_allows_off():
    with TestClient(app) as client:
        # Make sure relay-2 starts off.
        client.post("/api/relays/relay-2/off", auth=ADMIN_AUTH)
        # Disable relay-2.
        r = client.patch("/api/relays/relay-2", json={"enabled": False}, auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        # ON should be ignored: state stays OFF and a failed event is logged.
        r = client.post("/api/relays/relay-2/on", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_on"] is False

        events = client.get("/api/relays/relay-2/events?limit=5", auth=ADMIN_AUTH).json()
        assert any(e["success"] is False and "disabled" in (e["message"] or "") for e in events)

        # OFF must still succeed even when disabled (safety).
        r = client.post("/api/relays/relay-2/off", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert r.json()["is_on"] is False

        # Re-enable to leave fixture in good shape.
        client.patch("/api/relays/relay-2", json={"enabled": True}, auth=ADMIN_AUTH)


def test_admin_relay_controller_info_endpoint():
    with TestClient(app) as client:
        # Unauthenticated requests are rejected.
        assert client.get("/api/relays-controller").status_code == 401

        r = client.get("/api/relays-controller", auth=ADMIN_AUTH)
        assert r.status_code == 200
        info = r.json()
        assert info["mode"] in {"mock", "mcc_usb1208fs_plus"}
        assert isinstance(info["active_high"], bool)
        assert info["digital_port"]
        assert isinstance(info["bit_map"], dict)
        assert set(info["bit_map"].keys()) == {"relay-1", "relay-2", "relay-3"}
        assert isinstance(info["initialized"], bool)
        assert isinstance(info["latch"], int)


def test_admin_dashboard_html_has_relay_control_section():
    with TestClient(app) as client:
        r = client.get("/admin", auth=ADMIN_AUTH)
        assert r.status_code == 200
        # The new admin UI exposes a labeled Relay Control section and the
        # controller-info container that the dashboard JS populates.
        assert "Relay Control" in r.text
        assert "relayControllerInfo" in r.text
        assert "relayFeedback" in r.text
