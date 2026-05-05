from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MachineOut(BaseModel):
    id: str
    name: str
    enabled: bool
    interval_seconds: int
    activation_duration_seconds: int
    next_run_at: datetime | None


class MachineUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    activation_duration_seconds: int | None = Field(default=None, ge=1)


class ActivationEventOut(BaseModel):
    id: int
    machine_id: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    trigger_source: str
    message: str | None


class SensorReadingOut(BaseModel):
    id: int
    sensor_name: str
    temperature: float
    relative_humidity: float
    recorded_at: datetime
    raw_payload: str | None


class SensorLatestOut(BaseModel):
    sensor_name: str
    temperature: float
    relative_humidity: float
    recorded_at: datetime


class RoomSummaryOut(BaseModel):
    latest_by_sensor: list[SensorLatestOut]
    average_temperature: float | None
    average_relative_humidity: float | None
    sensor_count: int


class RelayOut(BaseModel):
    id: str
    name: str
    bit_index: int
    is_on: bool
    last_changed_at: datetime | None


class RelaySetIn(BaseModel):
    on: bool


class RelayEventOut(BaseModel):
    id: int
    relay_id: str
    state: bool
    action: str
    trigger_source: str
    success: bool
    message: str | None
    created_at: datetime


class DashboardStatusOut(BaseModel):
    machine: MachineOut
    last_activation: ActivationEventOut | None
    next_run_at: datetime | None
    seconds_until_next_run: int | None
    room: RoomSummaryOut
    relays: list[RelayOut] = Field(default_factory=list)


class ManualTriggerOut(BaseModel):
    event: ActivationEventOut


class HealthOut(BaseModel):
    status: str
    database: str
    scheduler_running: bool


class SystemLogOut(BaseModel):
    id: int
    level: str
    component: str
    message: str
    created_at: datetime


class DataSummaryOut(BaseModel):
    machines: int
    activation_events: int
    sensor_readings: int
    system_logs: int
    relays: int = 0
    relay_events: int = 0
