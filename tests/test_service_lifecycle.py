"""Background services must be idempotent to start.

app.main starts each service exactly once per process, but a duplicate start()
would silently double-read an Arduino or double-fire a schedule, so the guard
belongs in the services themselves.
"""
from __future__ import annotations

from app.config import Settings
from app.services.machine_controller import MockMachineController
from app.services.relay_controller import MockRelayController
from app.services.relay_scheduler import RelayScheduler
from app.services.scheduler import MachineScheduler
from app.services.sensor_service import SensorDevice, SensorIngestionManager


def _relay_controller() -> MockRelayController:
    return MockRelayController(bit_map={"relay-1": 0, "relay-2": 1, "relay-3": 2})


def test_sensor_manager_start_is_idempotent():
    """A second start() must not attach a second reader thread per device."""
    manager = SensorIngestionManager(
        devices=[SensorDevice("arduino-1", "/dev/null"), SensorDevice("arduino-2", "/dev/null")],
        baudrate=9600,
        timeout_seconds=0.1,
        simulator=True,
        machine_key="test-collector",
    )
    try:
        manager.start()
        manager.start()
        assert len(manager._threads) == 2
        assert manager.running
    finally:
        manager.stop()
    assert not manager.running


def test_sensor_manager_can_restart_after_stop():
    manager = SensorIngestionManager(
        devices=[SensorDevice("arduino-1", "/dev/null")],
        baudrate=9600,
        timeout_seconds=0.1,
        simulator=True,
        machine_key="test-collector",
    )
    manager.start()
    manager.stop()
    try:
        manager.start()
        assert len(manager._threads) == 1
        assert manager.running
    finally:
        manager.stop()


def test_one_sensor_thread_per_arduino():
    """Exactly one reader owns each configured serial port."""
    devices = [SensorDevice("arduino-1", "/dev/ttyACM0"), SensorDevice("arduino-2", "/dev/ttyACM1")]
    manager = SensorIngestionManager(
        devices=devices, baudrate=9600, timeout_seconds=0.1, simulator=True, machine_key="k"
    )
    try:
        manager.start()
        names = [thread.name for thread in manager._threads]
        assert sorted(names) == ["sensor-arduino-1", "sensor-arduino-2"]
    finally:
        manager.stop()


def test_relay_scheduler_start_is_idempotent():
    scheduler = RelayScheduler(_relay_controller(), machine_key="test-collector")
    try:
        scheduler.start()
        scheduler.start()
        assert scheduler.running
        assert len(scheduler.scheduler.get_jobs()) == 1
    finally:
        scheduler.stop()


def test_machine_scheduler_start_is_idempotent():
    scheduler = MachineScheduler(Settings(_env_file=None), MockMachineController())
    try:
        scheduler.start()
        scheduler.start()
        assert scheduler.running
        assert len(scheduler.scheduler.get_jobs()) == 1
    finally:
        scheduler.stop()


def test_relay_scheduler_ignores_other_machines_schedules():
    """A collector must never execute another machine's relay cycle."""
    scheduler = RelayScheduler(_relay_controller(), machine_key="collector-a")
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        # A schedule owned by a different machine resolves to that machine's row
        # and is returned unchanged rather than applied to local hardware.
        assert scheduler.apply_schedule_change(db, "relay-1", machine_key="collector-b") is None


def test_relay_controller_bit_masking_is_independent_per_relay():
    """Only one service drives the port latch, so per-bit writes must not
    disturb neighbouring relays."""
    controller = _relay_controller()
    controller.set_state("relay-1", True)
    controller.set_state("relay-3", True)
    assert controller.get_state("relay-1") is True
    assert controller.get_state("relay-2") is False
    assert controller.get_state("relay-3") is True
    controller.set_state("relay-1", False)
    assert controller.get_state("relay-1") is False
    assert controller.get_state("relay-3") is True
