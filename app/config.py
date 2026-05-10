from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Collector / machine_key validation. We accept lowercase letters, digits,
# dashes, underscores, and dots. Length 1..64.
MACHINE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def is_valid_machine_key(value: str | None) -> bool:
    if not value:
        return False
    return bool(MACHINE_KEY_RE.match(value))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Machine Research Backend"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/machine_research.db"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    admin_username: str = "admin"
    admin_password: str = "change-me-now"

    # Software version reported by the collector to the hub.
    software_version: str = "0.2.0"

    # Deployment mode:
    #   all_in_one  - single machine: dashboards + sensors + relays (default)
    #   hub         - home/server: serves dashboards, stores data, ingests from collector
    #   collector   - lab/Windows: drives Arduino + MCC hardware, pushes to hub via HTTP
    app_mode: Literal["all_in_one", "hub", "collector"] = "all_in_one"

    # Hub <-> collector shared secret. Required when running split deployment.
    collector_api_token: str = "change-me-collector-token"

    # Collector identity. Each physical collector machine MUST set its own
    # COLLECTOR_ID and COLLECTOR_NAME — the hub no longer ships with a
    # canonical machine list. The hub's COLLECTOR_ID/NAME values are used
    # only as the local default for all_in_one development.
    collector_id: str = "collector-1"
    collector_name: str = "Lab Collector"

    # Hub URL the collector will push data to (e.g. Tailscale URL of the home server)
    hub_base_url: str = "http://localhost:8000"

    # Collector loop tuning
    collector_push_interval_seconds: int = Field(default=10, ge=1, le=3600)
    collector_poll_interval_seconds: int = Field(default=5, ge=1, le=3600)
    collector_request_timeout_seconds: float = 10.0

    # Heartbeat staleness threshold for marking a collector offline.
    collector_stale_after_seconds: int = Field(default=60, ge=5, le=86400)

    default_machine_id: str = "machine-1"
    default_interval_seconds: int = Field(default=3600, ge=60)
    activation_duration_seconds: int = Field(default=5, ge=1)
    scheduler_timezone: str = "America/Chicago"

    machine_controller: Literal["mock", "wol", "command"] = "mock"
    wol_mac_address: str | None = None
    command_on: str | None = None

    sensor_simulator: bool = True
    arduino_1_port: str = "/dev/ttyACM0"
    arduino_1_name: str = "arduino-1"
    arduino_2_port: str = "/dev/ttyACM1"
    arduino_2_name: str = "arduino-2"
    arduino_baudrate: int = 9600
    sensor_read_timeout_seconds: float = 2.0

    relay_controller: Literal["mock", "mcc_usb1208fs_plus"] = "mock"
    mcc_board_num: int = 0
    mcc_digital_port: str = "FIRSTPORTB"
    relay_1_bit: int = Field(default=0, ge=0, le=7)
    relay_2_bit: int = Field(default=1, ge=0, le=7)
    relay_3_bit: int = Field(default=2, ge=0, le=7)
    relay_active_high: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def relay_bit_map(self) -> dict[str, int]:
        return {
            "relay-1": self.relay_1_bit,
            "relay-2": self.relay_2_bit,
            "relay-3": self.relay_3_bit,
        }

    @property
    def is_hub(self) -> bool:
        return self.app_mode in ("hub", "all_in_one")

    @property
    def is_collector(self) -> bool:
        return self.app_mode in ("collector", "all_in_one")

    @property
    def runs_local_hardware(self) -> bool:
        return self.app_mode in ("collector", "all_in_one")


@lru_cache
def get_settings() -> Settings:
    return Settings()
