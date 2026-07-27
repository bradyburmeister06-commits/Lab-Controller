from __future__ import annotations

import json
import random
import re
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.models import SensorReading, utcnow
from app.db.session import SessionLocal

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None


LINE_RE = re.compile(
    r"(?:temp|temperature)\s*[:=]\s*(?P<temp>-?\d+(?:\.\d+)?)\s*[,; ]+\s*(?:rh|humidity|relative_humidity)\s*[:=]\s*(?P<rh>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SensorDevice:
    name: str
    port: str


def parse_sensor_line(line: str) -> tuple[float, float]:
    stripped = line.strip()
    if not stripped:
        raise ValueError("Empty sensor line.")

    if stripped.startswith("{"):
        payload = json.loads(stripped)
        temp = payload.get("temp", payload.get("temperature"))
        rh = payload.get("rh", payload.get("humidity", payload.get("relative_humidity")))
        if temp is None or rh is None:
            raise ValueError("JSON sensor line must include temp/temperature and rh/humidity.")
        return validate_reading(float(temp), float(rh))

    match = LINE_RE.search(stripped)
    if not match:
        raise ValueError(f"Unsupported sensor line format: {line!r}")
    return validate_reading(float(match.group("temp")), float(match.group("rh")))


def validate_reading(temperature: float, relative_humidity: float) -> tuple[float, float]:
    if not -40 <= temperature <= 185:
        raise ValueError(f"Temperature out of expected range: {temperature}")
    if not 0 <= relative_humidity <= 100:
        raise ValueError(f"Relative humidity out of expected range: {relative_humidity}")
    return temperature, relative_humidity


def save_reading(
    db: Session,
    sensor_name: str,
    temperature: float,
    relative_humidity: float,
    raw_payload: str | None,
    machine_key: str | None = None,
) -> SensorReading:
    reading = SensorReading(
        sensor_name=sensor_name,
        machine_key=machine_key,
        temperature=temperature,
        relative_humidity=relative_humidity,
        raw_payload=raw_payload,
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return reading


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


class SensorIngestionManager:
    def __init__(
        self,
        devices: list[SensorDevice],
        baudrate: int,
        timeout_seconds: float,
        simulator: bool,
        machine_key: str | None = None,
    ) -> None:
        self.devices = devices
        self.baudrate = baudrate
        self.timeout_seconds = timeout_seconds
        self.simulator = simulator
        self.machine_key = machine_key
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads) and not self._stop.is_set()

    def start(self) -> None:
        # Idempotent: a second start() must not attach a second reader thread to
        # the same Arduino, which would double-record every reading.
        if self._threads:
            return
        self._stop.clear()
        for device in self.devices:
            target = self._simulate_device if self.simulator else self._read_serial_device
            thread = threading.Thread(target=target, args=(device,), daemon=True, name=f"sensor-{device.name}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    def _simulate_device(self, device: SensorDevice) -> None:
        base_temp = random.uniform(68, 74)
        base_rh = random.uniform(38, 55)
        while not self._stop.is_set():
            temp = round(base_temp + random.uniform(-1.5, 1.5), 2)
            rh = round(base_rh + random.uniform(-3.0, 3.0), 2)
            with SessionLocal() as db:
                save_reading(db, device.name, temp, rh, raw_payload="simulator", machine_key=self.machine_key)
            self._stop.wait(10)

    def _read_serial_device(self, device: SensorDevice) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed.")

        while not self._stop.is_set():
            try:
                with serial.Serial(device.port, self.baudrate, timeout=self.timeout_seconds) as ser:
                    while not self._stop.is_set():
                        raw = ser.readline().decode("utf-8", errors="replace").strip()
                        if not raw:
                            continue
                        try:
                            temp, rh = parse_sensor_line(raw)
                            with SessionLocal() as db:
                                save_reading(db, device.name, temp, rh, raw_payload=raw, machine_key=self.machine_key)
                        except ValueError:
                            continue
            except Exception:
                self._stop.wait(5)
