from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass
class RelayResult:
    success: bool
    message: str


class RelayController:
    """Abstract relay controller managing a set of bits on a single digital port.

    Implementations maintain the full output byte (latch) and apply individual
    bit changes by masking, so that toggling one relay never disturbs the
    other bits on the same port.
    """

    def __init__(self, bit_map: dict[str, int], active_high: bool = True) -> None:
        self.bit_map = dict(bit_map)
        self.active_high = active_high
        self._latch = 0
        self._lock = threading.Lock()

    @property
    def latch(self) -> int:
        return self._latch

    def initialize(self) -> None:
        """Configure hardware and force all relays to off. Safe to call repeatedly."""

    def set_state(self, relay_id: str, on: bool) -> RelayResult:
        if relay_id not in self.bit_map:
            return RelayResult(False, f"Unknown relay: {relay_id}")
        bit = self.bit_map[relay_id]
        with self._lock:
            level_high = on if self.active_high else (not on)
            new_latch = (self._latch | (1 << bit)) if level_high else (self._latch & ~(1 << bit))
            try:
                self._write_byte(new_latch)
            except Exception as exc:  # pragma: no cover - hardware dependent
                return RelayResult(False, f"Hardware write failed: {exc}")
            self._latch = new_latch
            return RelayResult(True, f"Relay {relay_id} set to {'on' if on else 'off'} (latch=0x{new_latch:02X})")

    def get_state(self, relay_id: str) -> bool:
        bit = self.bit_map[relay_id]
        level_high = bool(self._latch & (1 << bit))
        return level_high if self.active_high else (not level_high)

    def _write_byte(self, value: int) -> None:
        raise NotImplementedError


class MockRelayController(RelayController):
    def _write_byte(self, value: int) -> None:
        return None


class MccUsb1208FsPlusRelayController(RelayController):
    """Drives 3 relays via an MCC USB-1208FS-Plus DIO port using mcculw.

    NOTE: USB-1208FS-Plus DIO lines are TTL-level outputs. Do not drive
    relay coils directly from the device — use a relay board / driver
    with an opto-isolated input compatible with the DIO output limits.
    Port B lines are documented as high-current (24 mA) on this device.
    """

    def __init__(
        self,
        bit_map: dict[str, int],
        active_high: bool = True,
        board_num: int = 0,
        digital_port: str = "FIRSTPORTB",
    ) -> None:
        super().__init__(bit_map, active_high)
        self.board_num = board_num
        self.digital_port_name = digital_port
        self._mcculw_dio = None
        self._mcculw_enums = None
        self._port_type = None
        self._configured = False

    def initialize(self) -> None:
        # Optional import: mcculw is Windows-only; fall back gracefully.
        try:
            from mcculw import ul  # type: ignore[import-not-found]
            from mcculw.enums import DigitalIODirection, DigitalPortType  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - import path is platform dependent
            raise RuntimeError(
                "mcculw is not available. Install MCC Universal Library / InstaCal "
                "and `pip install mcculw` on Windows. Original error: " + str(exc)
            ) from exc

        port_type = getattr(DigitalPortType, self.digital_port_name, None)
        if port_type is None:  # pragma: no cover - configuration error
            raise ValueError(f"Unknown MCC digital port: {self.digital_port_name}")

        self._mcculw_dio = ul
        self._mcculw_enums = (DigitalIODirection, DigitalPortType)
        self._port_type = port_type

        ul.d_config_port(self.board_num, port_type, DigitalIODirection.OUT)
        # Force all-off using the configured active-high/low semantics.
        off_byte = 0x00 if self.active_high else 0xFF
        ul.d_out(self.board_num, port_type, off_byte)
        self._latch = off_byte
        self._configured = True

    def _write_byte(self, value: int) -> None:
        if not self._configured or self._mcculw_dio is None:  # pragma: no cover - misuse guard
            raise RuntimeError("MCC controller not initialized; call initialize() first.")
        self._mcculw_dio.d_out(self.board_num, self._port_type, int(value) & 0xFF)


class ArduinoSerialRelayController(RelayController):
    """Drive relays over an Arduino serial protocol."""

    def __init__(
        self,
        bit_map: dict[str, int],
        active_high: bool = True,
        primary_port: str = "/dev/ttyACM0",
        secondary_port: str | None = None,
        baud_rate: int = 115200,
        timeout_seconds: float = 2.0,
    ) -> None:
        super().__init__(bit_map, active_high)
        self.primary_port = primary_port
        self.secondary_port = secondary_port
        self.baud_rate = baud_rate
        self.timeout_seconds = timeout_seconds
        self._serial: Any = None
        self._configured = False
        self._connected_port: str | None = None

    def initialize(self) -> None:
        try:
            import serial  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pyserial is required for arduino_serial mode.") from exc
        candidate_ports = [self.primary_port]
        if self.secondary_port:
            candidate_ports.append(self.secondary_port)
        last_error: Exception | None = None
        for port in candidate_ports:
            try:
                conn = serial.Serial(port, self.baud_rate, timeout=self.timeout_seconds)
                self._serial = conn
                self._connected_port = port
                self._configured = True
                self._write_line("ALL_OFF")
                self._latch = 0x00 if self.active_high else 0xFF
                return
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(f"Unable to connect to Arduino on ports: {candidate_ports}. Last error: {last_error}")

    @property
    def connected_port(self) -> str | None:
        return self._connected_port

    def _write_line(self, command: str) -> None:
        if self._serial is None:
            raise RuntimeError("Arduino serial not initialized.")
        self._serial.write((command.strip() + "\n").encode("utf-8"))
        self._serial.flush()

    def _write_byte(self, value: int) -> None:
        if not self._configured:  # pragma: no cover
            raise RuntimeError("Arduino controller not initialized; call initialize() first.")
        self._write_line(f"SET RELAY_BYTE {int(value) & 0xFF}")


def build_relay_controller(settings: Settings) -> RelayController:
    if settings.relay_controller == "mcc_usb1208fs_plus":
        controller: RelayController = MccUsb1208FsPlusRelayController(
            bit_map=settings.relay_bit_map,
            active_high=settings.relay_active_high,
            board_num=settings.mcc_board_num,
            digital_port=settings.mcc_digital_port,
        )
    elif settings.relay_controller == "arduino_serial":
        controller = ArduinoSerialRelayController(
            bit_map=settings.relay_bit_map,
            active_high=settings.relay_active_high,
            primary_port=settings.arduino_1_port,
            secondary_port=settings.arduino_2_port,
            baud_rate=settings.arduino_baud_rate,
            timeout_seconds=settings.arduino_command_timeout_seconds,
        )
    else:
        controller = MockRelayController(
            bit_map=settings.relay_bit_map,
            active_high=settings.relay_active_high,
        )
    return controller
