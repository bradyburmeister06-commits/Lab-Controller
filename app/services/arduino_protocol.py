"""Arduino serial line format and the collector's internal reading object.

The canonical line the firmware emits is a single newline-terminated record of
comma-separated ``key=value`` pairs::

    chamber=chamber-a,temp=22.41,rh=48.10,uptime=930112,fw=1.4.2,actuator=on

`temp` and `rh` are required; everything else is optional. A JSON object on one
line carrying the same keys is also accepted, because earlier firmware builds in
the lab still emit it. See docs/arduino-collection.md for the full grammar.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Physically impossible readings are rejected outright: no sensor we support can
# produce them, so the record is corrupt rather than merely unusual.
TEMPERATURE_HARD_RANGE = (-40.0, 185.0)
HUMIDITY_HARD_RANGE = (0.0, 100.0)

# Inside the hard range but outside normal chamber operation. Kept, flagged, and
# left for the operator to judge.
TEMPERATURE_PLAUSIBLE_RANGE = (-10.0, 140.0)

QUALITY_OK = "ok"
QUALITY_SUSPECT_TEMPERATURE = "suspect_temperature"
QUALITY_SUSPECT_HUMIDITY = "suspect_humidity"

CHAMBER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_TEMP_KEYS = ("temp", "temperature", "temp_c", "temperature_c")
_RH_KEYS = ("rh", "humidity", "relative_humidity", "humidity_percent")
_CHAMBER_KEYS = ("chamber", "chamber_id", "id")
_UPTIME_KEYS = ("uptime", "uptime_ms", "millis")
_FIRMWARE_KEYS = ("fw", "firmware", "firmware_version", "version")
_ACTUATOR_KEYS = ("actuator", "actuator_status", "relay", "output")

# Boot banners, sensor init chatter and watchdog notices. An Arduino emits these
# on every reset, and they are not errors.
_NOISE_RE = re.compile(
    r"^(?:"
    r"[\x00-\x08\x0b-\x1f\x7f-\xff�]+"    # framing garbage from a mid-line reset
    r"|(?:\W*)(?:arduino|dht\d*|sht\d*|bme\d*|sensor|system|setup|boot|wdt|firmware)\b.*"
    r"|(?:\W*)(?:ready|booting|starting|initializing|init|reset|rebooting|ok)\W*"
    r")$",
    re.IGNORECASE,
)

# A banner never carries a temperature field, so this rescues data lines whose
# first token happens to start with a banner word.
_DATA_HINT_RE = re.compile(r"\b(?:temp|temperature|temp_c|temperature_c)\s*[:=]", re.IGNORECASE)


class SensorLineError(ValueError):
    """A serial line could not be turned into a reading."""


class ArduinoNoiseLine(SensorLineError):
    """A reset banner or init message. Expected, and logged at debug only."""


@dataclass(frozen=True)
class SensorReadingRecord:
    """One validated reading, identical in shape for simulator and hardware."""

    sensor_id: str
    temperature: float
    humidity_percent: float
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    local_record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    collector_id: str | None = None
    chamber_id: str | None = None
    firmware_version: str | None = None
    uptime_ms: int | None = None
    actuator_status: str | None = None
    quality_status: str = QUALITY_OK
    raw_line: str | None = None

    @property
    def is_suspect(self) -> bool:
        return self.quality_status != QUALITY_OK


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _tokenize(line: str) -> dict[str, Any]:
    if line.startswith("{"):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SensorLineError(f"Malformed JSON sensor line: {exc}") from exc
        if not isinstance(payload, dict):
            raise SensorLineError("JSON sensor line must be an object.")
        return {str(k).strip().lower(): v for k, v in payload.items()}

    fields: dict[str, Any] = {}
    for token in re.split(r"[,;]", line):
        token = token.strip()
        if not token:
            continue
        match = re.match(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<value>.*)$", token)
        if not match:
            raise SensorLineError(f"Sensor line token is not key=value: {token!r}")
        fields[match.group("key").lower()] = match.group("value").strip()
    if not fields:
        raise SensorLineError("Sensor line contained no key=value fields.")
    return fields


def _to_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SensorLineError(f"{label} is not numeric: {value!r}") from exc
    # NaN/inf survive float() but poison every downstream aggregate.
    if result != result or result in (float("inf"), float("-inf")):
        raise SensorLineError(f"{label} is not a finite number: {value!r}")
    return result


def _to_optional_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError) as exc:
        raise SensorLineError(f"{label} is not an integer: {value!r}") from exc
    if result < 0:
        raise SensorLineError(f"{label} cannot be negative: {value!r}")
    return result


def validate_chamber_id(value: Any) -> str:
    text = str(value).strip()
    if not CHAMBER_ID_RE.match(text):
        raise SensorLineError(f"Invalid chamber identifier: {value!r}")
    return text


def classify_quality(temperature: float, humidity_percent: float) -> str:
    """Range-check a reading, rejecting the impossible and flagging the unlikely."""
    low, high = HUMIDITY_HARD_RANGE
    if not low <= humidity_percent <= high:
        raise SensorLineError(f"Relative humidity out of range: {humidity_percent}")

    low, high = TEMPERATURE_HARD_RANGE
    if not low <= temperature <= high:
        raise SensorLineError(f"Temperature out of range: {temperature}")

    low, high = TEMPERATURE_PLAUSIBLE_RANGE
    if not low <= temperature <= high:
        return QUALITY_SUSPECT_TEMPERATURE
    # A rail-pinned humidity channel is the classic DHT wiring fault.
    if humidity_percent in HUMIDITY_HARD_RANGE:
        return QUALITY_SUSPECT_HUMIDITY
    return QUALITY_OK


def parse_reading_line(
    line: str,
    sensor_id: str,
    collector_id: str | None = None,
    expected_chamber_id: str | None = None,
) -> SensorReadingRecord:
    """Turn one complete serial line into a validated record.

    Raises `ArduinoNoiseLine` for reset banners and `SensorLineError` for
    anything malformed, incomplete, non-numeric or out of range.
    """
    stripped = line.strip()
    if not stripped:
        raise SensorLineError("Empty sensor line.")
    if _NOISE_RE.match(stripped) and not _DATA_HINT_RE.search(stripped):
        raise ArduinoNoiseLine(f"Arduino status/reset message: {stripped!r}")

    fields = _tokenize(stripped)

    raw_temp = _first_present(fields, _TEMP_KEYS)
    raw_rh = _first_present(fields, _RH_KEYS)
    if raw_temp is None or raw_rh is None:
        raise SensorLineError(f"Incomplete record, temperature and humidity are required: {stripped!r}")

    temperature = _to_float(raw_temp, "Temperature")
    humidity = _to_float(raw_rh, "Relative humidity")
    quality = classify_quality(temperature, humidity)

    raw_chamber = _first_present(fields, _CHAMBER_KEYS)
    # Absent chamber means single-chamber firmware; fall back to configuration.
    chamber_id = validate_chamber_id(raw_chamber) if raw_chamber is not None else expected_chamber_id
    if expected_chamber_id and raw_chamber is not None and chamber_id != expected_chamber_id:
        raise SensorLineError(
            f"Chamber identifier {chamber_id!r} does not match configured {expected_chamber_id!r}"
        )

    raw_actuator = _first_present(fields, _ACTUATOR_KEYS)
    raw_firmware = _first_present(fields, _FIRMWARE_KEYS)

    return SensorReadingRecord(
        sensor_id=sensor_id,
        collector_id=collector_id,
        chamber_id=chamber_id,
        temperature=temperature,
        humidity_percent=humidity,
        firmware_version=str(raw_firmware).strip() if raw_firmware is not None else None,
        uptime_ms=_to_optional_int(_first_present(fields, _UPTIME_KEYS), "Uptime"),
        actuator_status=str(raw_actuator).strip().lower() if raw_actuator is not None else None,
        quality_status=quality,
        raw_line=stripped,
    )
