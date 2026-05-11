from __future__ import annotations

import os


# Prevent tests from reading developer/production .env values.
os.environ["COLLECTOR_API_TOKEN"] = "change-me-collector-token"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "change-me-now"
os.environ["RELAY_CONTROLLER"] = "mock"
os.environ["MACHINE_CONTROLLER"] = "mock"
os.environ["APP_MODE"] = "all_in_one"


def pytest_configure() -> None:
    # Reset cached settings after forcing test env vars above.
    from app.config import get_settings

    get_settings.cache_clear()
