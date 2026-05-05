from __future__ import annotations

import threading
from dataclasses import dataclass

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


def build_relay_controller(settings: Settings) -> RelayController:
    if settings.relay_controller == "mcc_usb1208fs_plus":
        controller: RelayController = MccUsb1208FsPlusRelayController(
            bit_map=settings.relay_bit_map,
            active_high=settings.relay_active_high,
            board_num=settings.mcc_board_num,
            digital_port=settings.mcc_digital_port,
        )
    else:
        controller = MockRelayController(
            bit_map=settings.relay_bit_map,
            active_high=settings.relay_active_high,
        )
    return controller
