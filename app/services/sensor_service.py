from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.models import SensorReading, utcnow
from app.db.session import SessionLocal
from app.services.arduino_protocol import (
    ArduinoNoiseLine,
    SensorLineError,
    SensorReadingRecord,
    classify_quality,
    parse_reading_line,
)

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None


logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 2.0
RECONNECT_DELAY_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class SensorDevice:
    name: str
    port: str
    chamber_id: str | None = None


def parse_sensor_line(line: str) -> tuple[float, float]:
    """Backwards-compatible shim returning just (temperature, humidity)."""
    record = parse_reading_line(line, sensor_id="unknown")
    return record.temperature, record.humidity_percent


def save_reading(
    db: Session,
    sensor_name: str,
    temperature: float,
    relative_humidity: float,
    raw_payload: str | None,
    machine_key: str | None = None,
) -> SensorReading:
    """Persist a reading locally. This always happens before any upload attempt,
    so a reading survives a hub outage that starts a millisecond later."""
    reading = SensorReading(
        sensor_name=sensor_name,
        machine_key=machine_key,
        collector_id=machine_key,
        temperature=temperature,
        relative_humidity=relative_humidity,
        raw_payload=raw_payload,
    )
    db.add(reading)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(reading)
    return reading


def save_record(db: Session, record: SensorReadingRecord) -> SensorReading:
    """Persist a validated record. Re-validates so nothing hand-built bypasses
    the range checks the parser applies."""
    classify_quality(record.temperature, record.humidity_percent)
    return save_reading(
        db,
        sensor_name=record.sensor_id,
        temperature=record.temperature,
        relative_humidity=record.humidity_percent,
        raw_payload=record.raw_line,
        machine_key=record.collector_id,
    )


def latest_by_sensor(db: Session) -> list[SensorReading]:
    subq = (
        select(SensorReading.sensor_name, func.max(SensorReading.recorded_at).label("max_recorded_at"))
        .group_by(SensorReading.sensor_name)
        .subquery()
    )
    return list(
        db.execute(
            select(SensorReading)
            .join(
                subq,
                (SensorReading.sensor_name == subq.c.sensor_name)
                & (SensorReading.recorded_at == subq.c.max_recorded_at),
            )
            .order_by(SensorReading.sensor_name)
        ).scalars()
    )


def recent_readings(
    db: Session,
    sensor_name: str | None = None,
    hours: int = 24,
    limit: int = 1000,
    machine_key: str | None = None,
) -> list[SensorReading]:
    since = utcnow() - timedelta(hours=hours)
    stmt = select(SensorReading).where(SensorReading.recorded_at >= since)
    if sensor_name:
        stmt = stmt.where(SensorReading.sensor_name == sensor_name)
    if machine_key is not None:
        stmt = stmt.where(SensorReading.machine_key == machine_key)
    stmt = stmt.order_by(desc(SensorReading.recorded_at)).limit(limit)
    return list(db.execute(stmt).scalars())


class ArduinoReaderState:
    """Live status of one Arduino, readable from any thread."""

    def __init__(self, device: SensorDevice) -> None:
        self.device = device
        self._lock = threading.Lock()
        self.connected = False
        self.last_record: SensorReadingRecord | None = None
        self.last_error: str | None = None
        self.last_error_at = None
        self.connect_failures = 0
        self.malformed_lines = 0

    def mark_connected(self) -> None:
        with self._lock:
            self.connected = True
            self.connect_failures = 0

    def mark_disconnected(self, error: str | None = None) -> None:
        with self._lock:
            self.connected = False
            if error is not None:
                self.last_error = error
                self.last_error_at = utcnow()
                self.connect_failures += 1

    def mark_record(self, record: SensorReadingRecord) -> None:
        with self._lock:
            self.last_record = record

    def mark_malformed(self) -> None:
        with self._lock:
            self.malformed_lines += 1

    def snapshot(self) -> dict:
        with self._lock:
            record = self.last_record
            return {
                "sensor_id": self.device.name,
                "port": self.device.port,
                "connected": self.connected,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "connect_failures": self.connect_failures,
                "malformed_lines": self.malformed_lines,
                "last_reading_at": record.timestamp_utc if record else None,
                "last_temperature": record.temperature if record else None,
                "last_humidity_percent": record.humidity_percent if record else None,
                "last_quality_status": record.quality_status if record else None,
                "firmware_version": record.firmware_version if record else None,
            }


class SensorIngestionManager:
    def __init__(
        self,
        devices: list[SensorDevice],
        baudrate: int,
        timeout_seconds: float,
        simulator: bool,
        machine_key: str | None = None,
        serial_factory=None,
        reconnect_delay_seconds: float = RECONNECT_DELAY_SECONDS,
        sample_interval_seconds: float = 10.0,
    ) -> None:
        self.devices = devices
        self.baudrate = baudrate
        self.timeout_seconds = timeout_seconds
        self.simulator = simulator
        self.machine_key = machine_key
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.sample_interval_seconds = sample_interval_seconds
        self._serial_factory = serial_factory
        self.states: dict[str, ArduinoReaderState] = {d.name: ArduinoReaderState(d) for d in devices}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads) and not self._stop.is_set()

    def status(self) -> list[dict]:
        return [self.states[device.name].snapshot() for device in self.devices]

    def start(self) -> None:
        # Idempotent: a second start() must not attach a second reader thread to
        # the same Arduino, which would double-record every reading.
        if self._threads:
            return
        self._stop.clear()
        for device in self.devices:
            target = self._simulate_device if self.simulator else self._run_serial_reader
            thread = threading.Thread(target=target, args=(device,), daemon=True, name=f"sensor-{device.name}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    def _open_serial(self, device: SensorDevice):
        if self._serial_factory is not None:
            return self._serial_factory(device.port, self.baudrate, timeout=self.timeout_seconds)
        if serial is None:
            raise RuntimeError("pyserial is not installed.")
        return serial.Serial(device.port, self.baudrate, timeout=self.timeout_seconds)

    def _record_from_line(self, device: SensorDevice, raw: str) -> SensorReadingRecord:
        return parse_reading_line(
            raw,
            sensor_id=device.name,
            collector_id=self.machine_key,
            expected_chamber_id=device.chamber_id,
        )

    def _store(self, record: SensorReadingRecord) -> None:
        state = self.states[record.sensor_id]
        with SessionLocal() as db:
            save_record(db, record)
        state.mark_record(record)
        if record.is_suspect:
            logger.warning(
                "Sensor %s reading flagged %s: temp=%s rh=%s",
                record.sensor_id,
                record.quality_status,
                record.temperature,
                record.humidity_percent,
            )

    def _simulate_device(self, device: SensorDevice) -> None:
        state = self.states[device.name]
        state.mark_connected()
        base_temp = random.uniform(68, 74)
        base_rh = random.uniform(38, 55)
        while not self._stop.is_set():
            temp = round(base_temp + random.uniform(-1.5, 1.5), 2)
            rh = round(base_rh + random.uniform(-3.0, 3.0), 2)
            line = f"chamber={device.chamber_id or device.name},temp={temp},rh={rh},fw=simulator"
            try:
                self._store(self._record_from_line(device, line))
            except Exception:
                logger.exception("Simulator reading failed for %s", device.name)
            self._stop.wait(self.sample_interval_seconds)
        state.mark_disconnected()

    def _reconnect_wait(self, state: ArduinoReaderState) -> None:
        """Back off so a permanently absent COM port cannot spin the CPU."""
        delay = min(
            self.reconnect_delay_seconds * max(1, state.connect_failures),
            RECONNECT_DELAY_MAX_SECONDS,
        )
        self._stop.wait(delay)

    def _run_serial_reader(self, device: SensorDevice) -> None:
        """One Arduino's whole lifecycle. Never raises, so a failure here can
        never take down the reader thread for the other Arduino."""
        state = self.states[device.name]
        while not self._stop.is_set():
            try:
                connection = self._open_serial(device)
            except Exception as exc:
                # Covers a missing COM port, a permissions/locked-port error, and
                # a cable pulled between retries.
                state.mark_disconnected(f"{type(exc).__name__}: {exc}")
                logger.warning("Cannot open %s for %s: %s", device.port, device.name, exc)
                self._reconnect_wait(state)
                continue

            state.mark_connected()
            logger.info("Arduino %s connected on %s", device.name, device.port)
            try:
                self._pump(device, connection)
            except Exception as exc:
                state.mark_disconnected(f"{type(exc).__name__}: {exc}")
                logger.warning("Serial read failed for %s on %s: %s", device.name, device.port, exc)
            else:
                state.mark_disconnected()
            finally:
                self._close(device, connection)
            if not self._stop.is_set():
                self._reconnect_wait(state)
        state.mark_disconnected()

    def _pump(self, device: SensorDevice, connection) -> None:
        """Read complete lines until the port dies or we are asked to stop."""
        state = self.states[device.name]
        while not self._stop.is_set():
            data = connection.readline()
            if data == b"":
                # pyserial returns b"" on read timeout with the port still open;
                # only a genuine EOF/disconnect raises, which _run_serial_reader
                # catches. Keep waiting for the next line.
                continue
            if isinstance(data, bytes):
                raw = data.decode("utf-8", errors="replace").strip()
            else:
                raw = str(data).strip()
            if not raw:
                continue
            try:
                record = self._record_from_line(device, raw)
            except ArduinoNoiseLine as exc:
                logger.debug("Arduino %s status line: %s", device.name, exc)
                continue
            except SensorLineError as exc:
                state.mark_malformed()
                logger.debug("Malformed line from %s: %r (%s)", device.name, raw, exc)
                continue
            try:
                self._store(record)
            except Exception:
                logger.exception("Failed to store reading from %s", device.name)

    @staticmethod
    def _close(device: SensorDevice, connection) -> None:
        try:
            connection.close()
        except Exception:
            logger.debug("Error closing serial port for %s", device.name, exc_info=True)
