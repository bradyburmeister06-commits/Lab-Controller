from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Machine Research Backend"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/machine_research.db"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    admin_username: str = "admin"
    admin_password: str = "change-me-now"

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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
