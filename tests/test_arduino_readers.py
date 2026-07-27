"""Two Arduinos, two COM ports, one thread each.

The property under test throughout is isolation: whatever happens to one serial
port — never opens, dies mid-stream, emits garbage — the other Arduino keeps
recording, and the failed one keeps trying to come back.
"""
from __future__ import annotations

import threading
import time

import pytest
from serial import SerialException

from app.services import sensor_service
from app.services.sensor_service import SensorDevice, SensorIngestionManager

DISCONNECT = object()  # sentinel: script step that drops the connection


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeSerial:
    """Replays a scripted sequence of readline() results, cycling once the
    script runs out so a healthy board streams indefinitely like the real one.
    Use the DISCONNECT sentinel to script a mid-stream drop."""

    def __init__(self, port: str, steps: list) -> None:
        self.port = port
        self._steps = list(steps)
        self._index = 0
        self._lock = threading.Lock()
        self.closed = False

    def readline(self):
        with self._lock:
            step = self._steps[self._index % len(self._steps)]
            self._index += 1
        if step is DISCONNECT:
            raise SerialException(f"device reports readiness to read but returned no data ({self.port})")
        if isinstance(step, Exception):
            raise step
        time.sleep(0.002)
        return step

    def close(self) -> None:
        self.closed = True


class FakeSerialFactory:
    """Per-port scripts. Each entry is a list of "attempts"; attempt N is either
    an Exception (open fails) or a list of readline steps."""

    def __init__(self, scripts: dict[str, list]) -> None:
        self.scripts = {port: list(attempts) for port, attempts in scripts.items()}
        self.opens: list[str] = []
        self.instances: list[FakeSerial] = []
        self._lock = threading.Lock()

    def __call__(self, port: str, baudrate: int, timeout: float | None = None):
        with self._lock:
            self.opens.append(port)
            attempts = self.scripts.get(port)
            if not attempts:
                raise SerialException(f"could not open port {port}: no such file or directory")
            attempt = attempts.pop(0) if len(attempts) > 1 else attempts[0]
        if isinstance(attempt, Exception):
            raise attempt
        instance = FakeSerial(port, attempt)
        with self._lock:
            self.instances.append(instance)
        return instance

    def open_count(self, port: str) -> int:
        with self._lock:
            return self.opens.count(port)

    def instances_for(self, port: str) -> list[FakeSerial]:
        with self._lock:
            return [i for i in self.instances if i.port == port]


@pytest.fixture
def stored(monkeypatch):
    """Capture records instead of writing to the collector database."""
    records = []
    lock = threading.Lock()

    def fake_save_record(db, record):
        with lock:
            records.append(record)
        return record

    monkeypatch.setattr(sensor_service, "save_record", fake_save_record)
    return records


def build_manager(factory, devices=None, **kwargs) -> SensorIngestionManager:
    devices = devices or [
        SensorDevice("arduino-1", "COM3"),
        SensorDevice("arduino-2", "COM4"),
    ]
    kwargs.setdefault("reconnect_delay_seconds", 0.01)
    return SensorIngestionManager(
        devices=devices,
        baudrate=9600,
        timeout_seconds=0.05,
        simulator=False,
        machine_key="collector-1",
        serial_factory=factory,
        **kwargs,
    )


def names_of(records) -> set[str]:
    return {record.sensor_id for record in records}


def test_both_arduinos_are_read_independently(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [[b"chamber=chamber-a,temp=21.0,rh=40.0,fw=1.4.2\n"] * 3],
            "COM4": [[b"chamber=chamber-b,temp=22.0,rh=45.0,fw=1.4.2\n"] * 3],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: names_of(stored) == {"arduino-1", "arduino-2"})
        assert all(state["connected"] for state in manager.status())
    finally:
        manager.stop()

    by_sensor = {record.sensor_id: record for record in stored}
    assert by_sensor["arduino-1"].chamber_id == "chamber-a"
    assert by_sensor["arduino-2"].chamber_id == "chamber-b"
    assert by_sensor["arduino-1"].collector_id == "collector-1"


def test_one_arduino_failing_does_not_stop_the_other(stored):
    """COM3 never opens; COM4 must keep recording regardless."""
    factory = FakeSerialFactory(
        {
            "COM3": [SerialException("could not open port COM3")],
            "COM4": [[b"temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: "arduino-2" in names_of(stored))
        assert wait_for(lambda: factory.open_count("COM3") >= 2)  # still retrying
        status = {state["sensor_id"]: state for state in manager.status()}
        assert status["arduino-1"]["connected"] is False
        assert "could not open port COM3" in status["arduino-1"]["last_error"]
        assert status["arduino-2"]["connected"] is True
    finally:
        manager.stop()

    assert "arduino-1" not in names_of(stored)


def test_mid_stream_disconnect_leaves_the_other_reader_running(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [[b"temp=21.0,rh=40.0\n", DISCONNECT], SerialException("COM3 gone")],
            "COM4": [[b"temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: names_of(stored) == {"arduino-1", "arduino-2"})
        assert wait_for(lambda: manager.status()[0]["connected"] is False)
        status = {s["sensor_id"]: s for s in manager.status()}
        assert status["arduino-2"]["connected"] is True
        # The reading taken before the drop is retained as the last known value.
        assert status["arduino-1"]["last_temperature"] == 21.0
        assert "COM3" in status["arduino-1"]["last_error"]
    finally:
        manager.stop()


def test_both_arduinos_disconnecting_is_survivable(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [SerialException("COM3 gone")],
            "COM4": [SerialException("COM4 gone")],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: factory.open_count("COM3") >= 2 and factory.open_count("COM4") >= 2)
        assert manager.running
        for state in manager.status():
            assert state["connected"] is False
            assert state["last_error"] is not None
            assert state["connect_failures"] >= 1
    finally:
        manager.stop()
    assert not manager.running


def test_reader_reconnects_after_a_disconnection(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [
                SerialException("COM3 temporarily unavailable"),
                [b"temp=21.5,rh=41.0,fw=1.4.2\n"],
            ],
            "COM4": [[b"temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: "arduino-1" in names_of(stored))
        status = {s["sensor_id"]: s for s in manager.status()}
        assert status["arduino-1"]["connected"] is True
        # The failure that preceded the successful reopen is still visible.
        assert "temporarily unavailable" in status["arduino-1"]["last_error"]
        assert status["arduino-1"]["last_temperature"] == 21.5
    finally:
        manager.stop()


def test_com_port_access_error_is_retried_with_a_delay(stored):
    """A locked port (another process holds it) must back off, not spin."""
    factory = FakeSerialFactory(
        {
            "COM3": [SerialException("PermissionError(13, 'Access is denied.')")],
            "COM4": [SerialException("PermissionError(13, 'Access is denied.')")],
        }
    )
    manager = build_manager(factory, reconnect_delay_seconds=0.2)
    manager.start()
    try:
        assert wait_for(lambda: factory.open_count("COM3") >= 2)
        time.sleep(0.3)
        # With a 0.2s base delay and linear backoff, a spinning loop would be in
        # the hundreds of attempts by now.
        assert factory.open_count("COM3") < 10
        assert "Access is denied" in manager.status()[0]["last_error"]
    finally:
        manager.stop()


def test_malformed_and_reset_lines_do_not_kill_the_reader(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [
                [
                    b"Arduino ready\n",
                    b"DHT22 sensor init\n",
                    b"temp=abc,rh=40.0\n",
                    b"\xff\xfe garbage \xff\n",
                    b"temp=21.0\n",
                    b"temp=21.0,rh=40.0\n",
                ]
            ],
            "COM4": [[b"temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: "arduino-1" in names_of(stored))
        status = {s["sensor_id"]: s for s in manager.status()}
        assert status["arduino-1"]["malformed_lines"] >= 2
    finally:
        manager.stop()

    arduino_1 = [record for record in stored if record.sensor_id == "arduino-1"]
    assert all(record.temperature == 21.0 for record in arduino_1)


def test_swapped_com_ports_are_rejected_rather_than_mislabelled(stored):
    """arduino-1 is configured for chamber-a but chamber-b's board answers."""
    devices = [
        SensorDevice("arduino-1", "COM3", "chamber-a"),
        SensorDevice("arduino-2", "COM4", "chamber-b"),
    ]
    factory = FakeSerialFactory(
        {
            "COM3": [[b"chamber=chamber-b,temp=22.0,rh=45.0\n"]],
            "COM4": [[b"chamber=chamber-b,temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory, devices=devices)
    manager.start()
    try:
        assert wait_for(lambda: "arduino-2" in names_of(stored))
        assert wait_for(lambda: manager.status()[0]["malformed_lines"] >= 1)
    finally:
        manager.stop()
    assert "arduino-1" not in names_of(stored)


def test_flagged_readings_are_still_recorded(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [[b"temp=21.0,rh=100.0\n"]],
            "COM4": [[b"temp=180.0,rh=40.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    try:
        assert wait_for(lambda: names_of(stored) == {"arduino-1", "arduino-2"})
    finally:
        manager.stop()
    assert {record.quality_status for record in stored} == {"suspect_humidity", "suspect_temperature"}
    assert all(record.is_suspect for record in stored)


def test_serial_ports_are_closed_on_shutdown(stored):
    factory = FakeSerialFactory(
        {
            "COM3": [[b"temp=21.0,rh=40.0\n"]],
            "COM4": [[b"temp=22.0,rh=45.0\n"]],
        }
    )
    manager = build_manager(factory)
    manager.start()
    assert wait_for(lambda: names_of(stored) == {"arduino-1", "arduino-2"})
    manager.stop()

    assert wait_for(lambda: all(i.closed for i in factory.instances_for("COM3")))
    assert all(instance.closed for instance in factory.instances_for("COM4"))
    assert not manager.running


def test_simulator_produces_the_same_record_shape(stored):
    manager = SensorIngestionManager(
        devices=[SensorDevice("arduino-1", "COM3", "chamber-a")],
        baudrate=9600,
        timeout_seconds=0.05,
        simulator=True,
        machine_key="collector-1",
        sample_interval_seconds=0.01,
    )
    manager.start()
    try:
        assert wait_for(lambda: len(stored) >= 1)
    finally:
        manager.stop()

    record = stored[0]
    assert record.sensor_id == "arduino-1"
    assert record.chamber_id == "chamber-a"
    assert record.collector_id == "collector-1"
    assert record.firmware_version == "simulator"
    assert record.quality_status == "ok"
    assert record.local_record_id
    assert record.timestamp_utc.utcoffset().total_seconds() == 0
