from fastapi.testclient import TestClient

from app.main import app

ADMIN_AUTH = ("admin", "change-me-now")


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["database"] == "ok"
        # Status-script-relevant fields surface app/collector state.
        assert "app_mode" in payload
        assert "collector_id" in payload
        assert "relay_controller_mode" in payload
        # In all_in_one (test default) the collector agent is not constructed,
        # so the field is null rather than a bool.
        assert payload["app_mode"] in ("all_in_one", "hub", "collector")


def test_dashboard_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/public/dashboard")
        assert response.status_code == 200
        payload = response.json()
        assert payload["machine"]["id"] == "machine-1"
        assert "room" in payload


def test_admin_dashboard_requires_auth():
    with TestClient(app) as client:
        assert client.get("/admin").status_code == 401
        response = client.get("/admin", auth=ADMIN_AUTH)
        assert response.status_code == 200
        assert "Machine Research Sysadmin Dashboard" in response.text


def test_admin_api_requires_auth():
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        response = client.get("/api/dashboard", auth=ADMIN_AUTH)
        assert response.status_code == 200


def test_full_data_endpoints():
    with TestClient(app) as client:
        for path in ["/api/machines", "/api/activations", "/api/logs", "/api/data/summary", "/api/sensors/names"]:
            response = client.get(path, auth=ADMIN_AUTH)
            assert response.status_code == 200


def test_public_dashboard_is_served():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Machine Public Status" in response.text
        response = client.get("/public")
        assert response.status_code == 200
        assert "Machine Public Status" in response.text
        assert client.get("/static/index.html").status_code == 404
