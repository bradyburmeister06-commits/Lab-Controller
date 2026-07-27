"""Stage 4: application lifecycle and the admin relay-safety endpoints.

Startup, shutdown, and a failed startup must all end with the relays off.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app


ADMIN_AUTH = ("admin", "change-me-now")


@pytest.fixture(autouse=True)
def relays_off_after_each_test():
    yield
    if main_mod.relay_controller is not None:
        main_mod.relay_controller.all_off()


# --- lifecycle --------------------------------------------------------------


def test_startup_initializes_the_controller_and_turns_every_relay_off():
    main_mod.relay_controller.turn_on("relay-1")
    with TestClient(app):
        assert main_mod.relay_controller.initialized is True
        assert all(v is False for v in main_mod.relay_controller.get_states().values())


def test_shutdown_turns_every_relay_off():
    with TestClient(app) as client:
        r = client.post("/api/relays/relay-2/on", auth=ADMIN_AUTH)
        assert r.status_code == 200
        assert main_mod.relay_controller.get_states()["relay-2"] is True
    # Exiting the context runs the lifespan shutdown.
    assert all(v is False for v in main_mod.relay_controller.get_states().values())


def test_failed_startup_still_turns_relays_off(monkeypatch):
    main_mod.relay_controller.turn_on("relay-3")

    def boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main_mod, "init_db", boom)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass
    assert all(v is False for v in main_mod.relay_controller.get_states().values())


def test_startup_registers_the_activator_and_starts_services():
    with TestClient(app):
        assert app.state.relay_activator is not None
        assert app.state.relay_scheduler.running is True
        assert app.state.sensor_manager.running is True


def test_lifespan_can_run_twice_without_duplicating_scheduler_jobs():
    """A restart in the same process must not leave a second tick job behind."""
    with TestClient(app):
        pass
    with TestClient(app) as client:
        assert len(app.state.relay_scheduler.scheduler.get_jobs()) == 1
        assert client.get("/api/health").status_code == 200


# --- health -----------------------------------------------------------------


def test_health_reports_relay_safety_fields():
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["relay_controller_initialized"] is True
    assert sorted(body["relay_states"]) == ["relay-1", "relay-2", "relay-3"]
    assert body["relay_max_activation_seconds"] == 300
    assert body["active_relay_activations"] == []


# --- admin all-off ----------------------------------------------------------


def test_all_off_endpoint_requires_admin_auth():
    with TestClient(app) as client:
        assert client.post("/api/relays/all-off").status_code == 401


def test_all_off_endpoint_de_energises_everything():
    with TestClient(app) as client:
        client.post("/api/relays/relay-1/on", auth=ADMIN_AUTH)
        client.post("/api/relays/relay-2/on", auth=ADMIN_AUTH)
        assert main_mod.relay_controller.get_states()["relay-1"] is True

        r = client.post("/api/relays/all-off", auth=ADMIN_AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert body["relays_off"] == ["relay-1", "relay-2", "relay-3"]

        assert all(v is False for v in main_mod.relay_controller.get_states().values())
        relays = client.get("/api/relays", auth=ADMIN_AUTH).json()
        assert all(relay["is_on"] is False for relay in relays)


# --- admin timed activation -------------------------------------------------


def test_activate_endpoint_requires_admin_auth():
    with TestClient(app) as client:
        r = client.post("/api/relays/relay-1/activate", json={"duration_seconds": 0.01})
        assert r.status_code == 401


def test_activate_endpoint_runs_and_leaves_the_relay_off():
    with TestClient(app) as client:
        r = client.post(
            "/api/relays/relay-1/activate",
            json={"duration_seconds": 0.05},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["relay_id"] == "relay-1"
        assert body["completed"] is True
        assert body["elapsed_seconds"] >= 0
        assert main_mod.relay_controller.get_states()["relay-1"] is False


@pytest.mark.parametrize("duration", [0, -5])
def test_activate_endpoint_rejects_non_positive_durations(duration):
    with TestClient(app) as client:
        r = client.post(
            "/api/relays/relay-1/activate",
            json={"duration_seconds": duration},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 422


def test_activate_endpoint_rejects_durations_over_the_maximum():
    with TestClient(app) as client:
        r = client.post(
            "/api/relays/relay-1/activate",
            json={"duration_seconds": 100000},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 422


def test_activate_endpoint_rejects_an_unknown_relay():
    with TestClient(app) as client:
        r = client.post(
            "/api/relays/relay-99/activate",
            json={"duration_seconds": 0.01},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 404


def test_activate_endpoint_rejects_an_overlapping_activation():
    with TestClient(app) as client:
        activator = app.state.relay_activator

        async def hold():
            task = asyncio.create_task(activator.activate("relay-1", 5))
            await asyncio.sleep(0.05)
            return task

        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(hold())
            r = client.post(
                "/api/relays/relay-1/activate",
                json={"duration_seconds": 0.01},
                auth=ADMIN_AUTH,
            )
            assert r.status_code == 409
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                loop.run_until_complete(task)
        finally:
            loop.close()
        assert main_mod.relay_controller.get_states()["relay-1"] is False


def test_schedule_update_rejects_on_duration_over_the_maximum():
    with TestClient(app) as client:
        r = client.patch(
            "/api/relays/relay-1/schedule",
            json={"on_duration_seconds": 86000},
            auth=ADMIN_AUTH,
        )
        assert r.status_code == 422
        assert "maximum activation" in r.json()["detail"]
