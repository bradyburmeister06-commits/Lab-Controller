import pytest

from app.services.arduino_protocol import (
    QUALITY_OK,
    QUALITY_SUSPECT_HUMIDITY,
    QUALITY_SUSPECT_TEMPERATURE,
    ArduinoNoiseLine,
    SensorLineError,
    parse_reading_line,
)
from app.services.sensor_service import parse_sensor_line


# --- backwards-compatible tuple API -----------------------------------------


def test_parse_key_value_sensor_line():
    temp, rh = parse_sensor_line("temp=72.4,rh=48.1")
    assert temp == 72.4
    assert rh == 48.1


def test_parse_json_sensor_line():
    temp, rh = parse_sensor_line('{"temperature":70.2,"humidity":51.0}')
    assert temp == 70.2
    assert rh == 51.0


def test_rejects_bad_humidity():
    with pytest.raises(ValueError):
        parse_sensor_line("temp=72.4,rh=148.1")


# --- full record parsing ------------------------------------------------------


def test_parses_full_canonical_line():
    record = parse_reading_line(
        "chamber=chamber-a,temp=22.41,rh=48.10,uptime=930112,fw=1.4.2,actuator=on",
        sensor_id="arduino-1",
        collector_id="collector-1",
    )
    assert record.sensor_id == "arduino-1"
    assert record.collector_id == "collector-1"
    assert record.chamber_id == "chamber-a"
    assert record.temperature == 22.41
    assert record.humidity_percent == 48.10
    assert record.uptime_ms == 930112
    assert record.firmware_version == "1.4.2"
    assert record.actuator_status == "on"
    assert record.quality_status == QUALITY_OK
    assert record.raw_line.startswith("chamber=chamber-a")


def test_timestamp_is_utc_and_record_id_is_unique():
    first = parse_reading_line("temp=21,rh=40", sensor_id="arduino-1")
    second = parse_reading_line("temp=21,rh=40", sensor_id="arduino-1")
    assert first.timestamp_utc.utcoffset().total_seconds() == 0
    assert first.local_record_id != second.local_record_id
    assert len(first.local_record_id) == 32


def test_optional_fields_default_to_none():
    record = parse_reading_line("temp=21.0,rh=40.0", sensor_id="arduino-2")
    assert record.chamber_id is None
    assert record.uptime_ms is None
    assert record.firmware_version is None
    assert record.actuator_status is None


def test_missing_chamber_falls_back_to_configured_chamber():
    record = parse_reading_line("temp=21.0,rh=40.0", sensor_id="arduino-1", expected_chamber_id="chamber-a")
    assert record.chamber_id == "chamber-a"


def test_extra_unknown_fields_are_ignored():
    record = parse_reading_line("temp=21.0,rh=40.0,dewpoint=9.1,co2=415", sensor_id="arduino-1")
    assert record.temperature == 21.0
    assert record.humidity_percent == 40.0


def test_json_form_supports_the_same_fields():
    record = parse_reading_line(
        '{"chamber":"chamber-b","temp":20.5,"rh":44.0,"fw":"1.4.2","uptime":12,"actuator":"OFF"}',
        sensor_id="arduino-2",
    )
    assert record.chamber_id == "chamber-b"
    assert record.uptime_ms == 12
    assert record.actuator_status == "off"


# --- rejection ----------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "temp=21.0",                       # humidity missing
        "rh=40.0",                         # temperature missing
        "chamber=chamber-a,fw=1.4.2",      # no measurements at all
        "temp=21.0,rh=",                   # humidity present but empty
    ],
)
def test_rejects_incomplete_records(line):
    with pytest.raises(SensorLineError):
        parse_reading_line(line, sensor_id="arduino-1")


@pytest.mark.parametrize(
    "line",
    [
        "temp=abc,rh=40.0",
        "temp=21.0,rh=four",
        "temp=NaN,rh=40.0",
        "temp=inf,rh=40.0",
        "temp=21.0,rh=40.0,uptime=later",
        '{"temp":21.0,"rh":"wet"}',
        "{not json at all",
        "temp 21.0 rh 40.0",               # not key=value
    ],
)
def test_rejects_invalid_numeric_and_malformed_fields(line):
    with pytest.raises(SensorLineError):
        parse_reading_line(line, sensor_id="arduino-1")


@pytest.mark.parametrize("rh", [-0.5, 100.1, 148.1, 1000])
def test_rejects_humidity_outside_zero_to_one_hundred(rh):
    with pytest.raises(SensorLineError):
        parse_reading_line(f"temp=21.0,rh={rh}", sensor_id="arduino-1")


@pytest.mark.parametrize("temp", [-273.15, -41, 186, 5000])
def test_rejects_unreasonable_temperatures(temp):
    with pytest.raises(SensorLineError):
        parse_reading_line(f"temp={temp},rh=40.0", sensor_id="arduino-1")


def test_rejects_invalid_chamber_identifier():
    with pytest.raises(SensorLineError):
        parse_reading_line("chamber=bad chamber!,temp=21.0,rh=40.0", sensor_id="arduino-1")


def test_rejects_chamber_that_does_not_match_configuration():
    """Catches two Arduinos wired to swapped COM ports."""
    with pytest.raises(SensorLineError):
        parse_reading_line(
            "chamber=chamber-b,temp=21.0,rh=40.0",
            sensor_id="arduino-1",
            expected_chamber_id="chamber-a",
        )


# --- flagging (kept, not rejected) -------------------------------------------


@pytest.mark.parametrize("temp", [-20.0, 150.0, 184.0])
def test_flags_implausible_but_possible_temperature(temp):
    record = parse_reading_line(f"temp={temp},rh=40.0", sensor_id="arduino-1")
    assert record.quality_status == QUALITY_SUSPECT_TEMPERATURE
    assert record.is_suspect


@pytest.mark.parametrize("rh", [0.0, 100.0])
def test_flags_rail_pinned_humidity(rh):
    record = parse_reading_line(f"temp=21.0,rh={rh}", sensor_id="arduino-1")
    assert record.quality_status == QUALITY_SUSPECT_HUMIDITY
    assert record.is_suspect


# --- reset / noise messages ---------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Arduino ready",
        "ARDUINO UNO R3 booting",
        "DHT22 sensor init",
        "System reset",
        "setup() complete",
        "ready",
        "OK",
        "���",
    ],
)
def test_reset_and_boot_messages_are_reported_as_noise(line):
    with pytest.raises(ArduinoNoiseLine):
        parse_reading_line(line, sensor_id="arduino-1")


def test_noise_words_do_not_swallow_a_real_reading():
    record = parse_reading_line("sensor=dht22,temp=21.0,rh=40.0", sensor_id="arduino-1")
    assert record.temperature == 21.0
