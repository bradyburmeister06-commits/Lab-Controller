from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    # Sync queue tuning. Batches are capped on both ends: the collector never
    # sends more than sync_batch_size, the hub rejects anything over
    # hub_max_batch_size, and the collector's cap must stay under the hub's.
    collector_sync_batch_size: int = Field(default=200, ge=1, le=1000)
    hub_max_batch_size: int = Field(default=500, ge=1, le=5000)
    # Exponential backoff between failed upload attempts for one stream.
    collector_sync_backoff_base_seconds: float = Field(default=2.0, ge=0.1, le=600.0)
    collector_sync_backoff_max_seconds: float = Field(default=300.0, ge=1.0, le=86400.0)

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
    # Optional. When set, a chamber id on the wire must match, which catches a
    # swapped COM port configuration instead of silently mislabelling data.
    arduino_1_chamber_id: str | None = None
    arduino_2_chamber_id: str | None = None
    sensor_reconnect_delay_seconds: float = Field(default=2.0, ge=0.1, le=300.0)

    relay_controller: Literal["mock", "mcc_usb1208fs_plus"] = "mock"
    # Windows-only MCC settings. Parsed in every mode so a shared .env stays
    # valid, but only read by MccUsb1208FsPlusController, which is never built
    # in hub mode.
    mcc_board_num: int = Field(default=0, ge=0, le=99)
    mcc_digital_port: str = "FIRSTPORTB"
    relay_1_bit: int = Field(default=0, ge=0, le=7)
    relay_2_bit: int = Field(default=1, ge=0, le=7)
    relay_3_bit: int = Field(default=2, ge=0, le=7)
    relay_active_high: bool = True

    # Hardware protection: no single relay activation may exceed this, whether
    # it came from the API, the local scheduler, or a hub command.
    relay_max_activation_seconds: int = Field(default=300, ge=1, le=86400)

    @field_validator("mcc_digital_port", mode="after")
    @classmethod
    def _validate_mcc_port(cls, value: str) -> str:
        port = value.strip().upper()
        if not port:
            raise ValueError("MCC_DIGITAL_PORT must not be empty.")
        return port

    @model_validator(mode="after")
    def _validate_relay_bits(self) -> "Settings":
        bits = [self.relay_1_bit, self.relay_2_bit, self.relay_3_bit]
        if len(set(bits)) != len(bits):
            raise ValueError(
                "RELAY_1_BIT, RELAY_2_BIT and RELAY_3_BIT must be distinct; "
                f"got {bits}. Two relays on one bit would switch together."
            )
        return self

    @field_validator("arduino_1_chamber_id", "arduino_2_chamber_id", mode="after")
    @classmethod
    def _blank_chamber_id_is_unset(cls, value: str | None) -> str | None:
        # An empty ARDUINO_n_CHAMBER_ID= in .env must mean "no chamber check",
        # not "the chamber named empty string".
        return value.strip() or None if value else None

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
