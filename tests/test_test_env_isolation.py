from app.config import get_settings


def test_pytest_uses_isolated_env_defaults():
    settings = get_settings()
    assert settings.collector_api_token == "change-me-collector-token"
    assert settings.admin_username == "admin"
    assert settings.admin_password == "change-me-now"
    assert settings.relay_controller == "mock"
