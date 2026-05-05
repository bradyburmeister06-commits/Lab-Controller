import pytest

from app.services.sensor_service import parse_sensor_line


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
