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
    description: str | None = None
    bit_index: int
    is_on: bool
    enabled: bool = True
    display_order: int = 0
    last_changed_at: datetime | None


class RelaySetIn(BaseModel):
    on: bool


class RelayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=999)


class RelayScheduleOut(BaseModel):
    relay_id: str
    enabled: bool
    on_duration_seconds: int
    off_duration_seconds: int
    next_run_at: datetime | None
    current_phase: str
    updated_at: datetime | None = None


class RelayScheduleUpdate(BaseModel):
    enabled: bool | None = None
    on_duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    off_duration_seconds: int | None = Field(default=None, ge=1, le=86400)


class RelayControllerInfoOut(BaseModel):
    mode: str
    active_high: bool
    board_num: int
    digital_port: str
    bit_map: dict[str, int]
    initialized: bool
    latch: int


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
    relay_schedules: list[RelayScheduleOut] = Field(default_factory=list)


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


# --- Collector / hub split-mode schemas ---


class CollectorHeartbeatIn(BaseModel):
    collector_id: str
    name: str | None = None
    mode: str | None = None
    host: str | None = None
    relay_controller_mode: str | None = None
    relay_controller_initialized: bool | None = None
    status_message: str | None = None


class CollectorOut(BaseModel):
    id: str
    name: str
    mode: str | None = None
    host: str | None = None
    last_heartbeat_at: datetime | None = None
    last_status_message: str | None = None
    relay_controller_mode: str | None = None
    relay_controller_initialized: bool = False
    online: bool = False
    seconds_since_heartbeat: int | None = None


class CollectorSensorReadingIn(BaseModel):
    sensor_name: str
    temperature: float
    relative_humidity: float
    recorded_at: datetime | None = None
    raw_payload: str | None = None


class CollectorSensorBatchIn(BaseModel):
    collector_id: str
    readings: list[CollectorSensorReadingIn] = Field(default_factory=list)


class CollectorRelayEventIn(BaseModel):
    relay_id: str
    state: bool
    action: str = "set"
    trigger_source: str = "collector"
    success: bool = True
    message: str | None = None
    occurred_at: datetime | None = None


class CollectorRelayBatchIn(BaseModel):
    collector_id: str
    events: list[CollectorRelayEventIn] = Field(default_factory=list)
    relay_states: dict[str, bool] = Field(default_factory=dict)


class CollectorCommandOut(BaseModel):
    id: int
    relay_id: str | None = None
    command_type: str
    payload: str | None = None
    created_at: datetime


class CollectorPollOut(BaseModel):
    relays: list[RelayOut]
    relay_schedules: list[RelayScheduleOut]
    commands: list[CollectorCommandOut] = Field(default_factory=list)


class CollectorCommandAckIn(BaseModel):
    collector_id: str
    command_id: int
    success: bool = True
    message: str | None = None
