from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


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
    machine_key: str | None = None


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
    machine_key: str
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


class RelayActivateIn(BaseModel):
    # Zero and negative durations are rejected here as well as in
    # RelayActivator, so a bad request never reaches the hardware path.
    duration_seconds: float = Field(gt=0, le=86400)


class RelayActivationOut(BaseModel):
    relay_id: str
    requested_seconds: float
    elapsed_seconds: float
    started_at: datetime
    ended_at: datetime
    completed: bool
    message: str


class RelayAllOffOut(BaseModel):
    success: bool
    relays_off: list[str]
    message: str


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
    machine_key: str | None = None


class DashboardStatusOut(BaseModel):
    machine: MachineOut
    last_activation: ActivationEventOut | None
    next_run_at: datetime | None
    seconds_until_next_run: int | None
    room: RoomSummaryOut
    relays: list[RelayOut] = Field(default_factory=list)
    relay_schedules: list[RelayScheduleOut] = Field(default_factory=list)
    collectors: list["CollectorOut"] = Field(default_factory=list)


class ManualTriggerOut(BaseModel):
    event: ActivationEventOut


class HealthOut(BaseModel):
    status: str
    database: str
    scheduler_running: bool
    app_mode: str | None = None
    collector_id: str | None = None
    collector_agent_running: bool | None = None
    relay_controller_mode: str | None = None
    sensor_manager_running: bool | None = None
    relay_scheduler_running: bool | None = None
    hub_base_url: str | None = None
    last_sync_at: datetime | None = None
    pending_readings: int | None = None
    pending_relay_events: int | None = None
    relay_controller_initialized: bool | None = None
    relay_states: dict[str, bool] | None = None
    relay_max_activation_seconds: int | None = None
    active_relay_activations: list[str] | None = None


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
    collectors: int = 0


# --- Collector / hub split-mode schemas ---


class CollectorRegisterIn(BaseModel):
    collector_id: str
    name: str | None = None
    display_name: str | None = None
    mode: str | None = None
    host: str | None = None
    hostname: str | None = None
    software_version: str | None = None
    relay_controller_mode: str | None = None
    relay_controller_initialized: bool | None = None
    runtime_state: str | None = None


class CollectorHeartbeatIn(BaseModel):
    collector_id: str
    name: str | None = None
    display_name: str | None = None
    mode: str | None = None
    host: str | None = None
    hostname: str | None = None
    software_version: str | None = None
    relay_controller_mode: str | None = None
    relay_controller_initialized: bool | None = None
    runtime_state: str | None = None
    status_message: str | None = None


class CollectorOut(BaseModel):
    id: str
    machine_key: str
    name: str
    display_name: str
    role: str = "collector"
    status: str = "unknown"
    is_enabled: bool = True
    mode: str | None = None
    host: str | None = None
    hostname: str | None = None
    last_seen_ip: str | None = None
    software_version: str | None = None
    last_heartbeat_at: datetime | None = None
    last_status_message: str | None = None
    relay_controller_mode: str | None = None
    relay_controller_initialized: bool = False
    runtime_state: str | None = None
    online: bool = False
    stale: bool = False
    seconds_since_heartbeat: int | None = None


class CollectorUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    is_enabled: bool | None = None
    role: str | None = Field(default=None, min_length=1, max_length=32)


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


# --- Stage 3 sync-queue batch ingestion ---

# Mirrors arduino_protocol's hard ranges. A reading outside these is corrupt
# rather than merely unusual, so the hub refuses to store it.
READING_TEMPERATURE_RANGE = (-40.0, 185.0)
READING_HUMIDITY_RANGE = (0.0, 100.0)

LOCAL_RECORD_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"


class SyncReadingIn(BaseModel):
    """One locally-stored sensor reading offered to the hub for ingestion."""

    local_record_id: str = Field(pattern=LOCAL_RECORD_ID_RE)
    sensor_name: str = Field(min_length=1, max_length=64)
    chamber_id: str | None = Field(default=None, max_length=64)
    temperature: float = Field(ge=READING_TEMPERATURE_RANGE[0], le=READING_TEMPERATURE_RANGE[1])
    relative_humidity: float = Field(ge=READING_HUMIDITY_RANGE[0], le=READING_HUMIDITY_RANGE[1])
    recorded_at: datetime | None = None
    raw_payload: str | None = Field(default=None, max_length=4000)


class SyncReadingBatchIn(BaseModel):
    collector_id: str
    readings: list[SyncReadingIn] = Field(default_factory=list)


class SyncRelayEventIn(BaseModel):
    local_record_id: str = Field(pattern=LOCAL_RECORD_ID_RE)
    relay_id: str = Field(min_length=1, max_length=64)
    state: bool
    action: str = Field(default="set", max_length=32)
    trigger_source: str = Field(default="collector", max_length=32)
    success: bool = True
    message: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None


class SyncRelayEventBatchIn(BaseModel):
    collector_id: str
    events: list[SyncRelayEventIn] = Field(default_factory=list)
    relay_states: dict[str, bool] = Field(default_factory=dict)


class SyncRejectedRecord(BaseModel):
    local_record_id: str
    reason: str


class SyncBatchOut(BaseModel):
    """Result of one batch.

    ``duplicates`` are a success: the record is already on the hub, so the
    collector must mark it synced rather than retrying it forever.
    """

    collector_id: str
    accepted: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    rejected: list[SyncRejectedRecord] = Field(default_factory=list)
    accepted_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0

    @model_validator(mode="after")
    def _fill_counts(self) -> "SyncBatchOut":
        self.accepted_count = len(self.accepted)
        self.duplicate_count = len(self.duplicates)
        self.rejected_count = len(self.rejected)
        return self


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


# Resolve forward reference for nested CollectorOut inside DashboardStatusOut.
DashboardStatusOut.model_rebuild()
