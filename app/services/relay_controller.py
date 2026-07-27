from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.config import Settings


logger = logging.getLogger("app.relay_controller")


class RelayError(RuntimeError):
    """Base class for relay hardware/configuration failures."""


class RelayConfigError(RelayError):
    """The relay controller is misconfigured (unknown relay, bad port, bad bit)."""


class RelayConnectionError(RelayError):
    """The relay hardware could not be reached or configured."""


@dataclass
class RelayResult:
    success: bool
    message: str


class RelayController:
    """Abstract relay controller managing a set of bits on a single digital port.

    Implementations maintain the full output byte (latch) and apply individual
    bit changes by masking, so that toggling one relay never disturbs the
    other bits on the same port.

    Two call styles are supported deliberately:

    - ``turn_on`` / ``turn_off`` / ``all_off`` raise :class:`RelayError` on
      failure. Fail-safe activation paths use these so a failed write can never
      be mistaken for a relay that is actually off.
    - ``set_state`` returns a :class:`RelayResult`. The audited write path in
      ``relay_service.apply_state`` uses it, because a failed hardware write
      still has to be recorded as a relay event rather than aborting the
      request.
    """

    def __init__(self, bit_map: dict[str, int], active_high: bool = True) -> None:
        for relay_id, bit in bit_map.items():
            if not isinstance(bit, int) or not 0 <= bit <= 7:
                raise RelayConfigError(
                    f"Relay {relay_id!r} bit must be an integer 0-7, got {bit!r}."
                )
        duplicates = len(bit_map) - len(set(bit_map.values()))
        if duplicates:
            raise RelayConfigError(
                f"Relay bit assignments must be unique; got {bit_map!r}."
            )
        self.bit_map = dict(bit_map)
        self.active_high = active_high
        self._latch = 0
        self._lock = threading.RLock()
        self._configured = False

    @property
    def latch(self) -> int:
        return self._latch

    @property
    def initialized(self) -> bool:
        return self._configured

    @property
    def off_latch(self) -> int:
        """Latch value with every *mapped* relay de-energised.

        Only mapped bits are touched: an active-low board must not have its
        unmapped port lines driven high just to reach a safe state.
        """
        if self.active_high:
            return 0
        mask = 0
        for bit in self.bit_map.values():
            mask |= 1 << bit
        return mask

    def initialize(self) -> None:
        """Configure hardware and force all relays off. Safe to call repeatedly."""
        self._configured = True
        self.all_off()

    def turn_on(self, relay_id: str) -> None:
        self._set_checked(relay_id, True)

    def turn_off(self, relay_id: str) -> None:
        self._set_checked(relay_id, False)

    def all_off(self) -> None:
        """Drive every mapped relay off in a single port write."""
        with self._lock:
            target = self.off_latch
            try:
                self._write_byte(target)
            except Exception as exc:
                raise RelayConnectionError(f"all_off failed: {exc}") from exc
            self._latch = target

    def get_states(self) -> dict[str, bool]:
        return {relay_id: self.get_state(relay_id) for relay_id in self.bit_map}

    def health(self) -> dict:
        return {
            "controller": type(self).__name__,
            "initialized": self._configured,
            "active_high": self.active_high,
            "latch": self._latch & 0xFF,
            "bit_map": dict(self.bit_map),
            "states": self.get_states(),
            "any_on": any(self.get_states().values()),
        }

    def set_state(self, relay_id: str, on: bool) -> RelayResult:
        if relay_id not in self.bit_map:
            return RelayResult(False, f"Unknown relay: {relay_id}")
        try:
            new_latch = self._set_checked(relay_id, on)
        except RelayError as exc:
            return RelayResult(False, f"Hardware write failed: {exc}")
        return RelayResult(
            True, f"Relay {relay_id} set to {'on' if on else 'off'} (latch=0x{new_latch:02X})"
        )

    def get_state(self, relay_id: str) -> bool:
        bit = self.bit_map[relay_id]
        level_high = bool(self._latch & (1 << bit))
        return level_high if self.active_high else (not level_high)

    def _set_checked(self, relay_id: str, on: bool) -> int:
        if relay_id not in self.bit_map:
            raise RelayConfigError(f"Unknown relay: {relay_id}")
        bit = self.bit_map[relay_id]
        with self._lock:
            level_high = on if self.active_high else (not on)
            new_latch = (self._latch | (1 << bit)) if level_high else (self._latch & ~(1 << bit))
            try:
                self._write_byte(new_latch)
            except Exception as exc:
                raise RelayConnectionError(
                    f"Write for relay {relay_id} failed: {exc}"
                ) from exc
            self._latch = new_latch
            return new_latch

    def _write_byte(self, value: int) -> None:
        raise NotImplementedError


class MockRelayController(RelayController):
    def _write_byte(self, value: int) -> None:
        return None


class MccUsb1208FsPlusController(RelayController):
    """Drives 3 relays via an MCC USB-1208FS-Plus DIO port using mcculw.

    ``mcculw`` is Windows-only and is imported inside :meth:`initialize`, never
    at module scope, so hub/Linux/mock deployments never load the driver.

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
        if board_num < 0:
            raise RelayConfigError(f"MCC board number must be >= 0, got {board_num}.")
        self.board_num = board_num
        self.digital_port_name = digital_port
        self._mcculw_dio = None
        self._port_type = None

    def initialize(self) -> None:
        try:
            from mcculw import ul  # type: ignore[import-not-found]
            from mcculw.enums import DigitalIODirection, DigitalPortType  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - import path is platform dependent
            raise RelayConnectionError(
                "mcculw is not available. Install MCC Universal Library / InstaCal "
                "and `pip install mcculw` on Windows. Original error: " + str(exc)
            ) from exc

        port_type = getattr(DigitalPortType, self.digital_port_name, None)
        if port_type is None:
            raise RelayConfigError(f"Unknown MCC digital port: {self.digital_port_name}")

        try:
            ul.d_config_port(self.board_num, port_type, DigitalIODirection.OUT)
        except Exception as exc:
            raise RelayConnectionError(
                f"Could not configure MCC board {self.board_num} port "
                f"{self.digital_port_name} for output: {exc}"
            ) from exc

        self._mcculw_dio = ul
        self._port_type = port_type
        self._configured = True
        # Never inherit the port's power-on state: force a known safe latch.
        self.all_off()

    def _write_byte(self, value: int) -> None:
        if not self._configured or self._mcculw_dio is None:
            raise RelayConnectionError(
                "MCC controller not initialized; call initialize() first."
            )
        self._mcculw_dio.d_out(self.board_num, self._port_type, int(value) & 0xFF)


def build_relay_controller(settings: Settings) -> RelayController:
    if settings.relay_controller == "mcc_usb1208fs_plus":
        controller: RelayController = MccUsb1208FsPlusController(
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


def safe_all_off(controller: RelayController | None, reason: str) -> bool:
    """Best-effort de-energise used on shutdown and error paths.

    Never raises: the callers are already handling a failure, and an exception
    here would mask it.
    """
    if controller is None:
        return False
    try:
        controller.all_off()
    except Exception:
        logger.exception("emergency all_off failed (%s)", reason)
        return False
    logger.info("all relays off (%s)", reason)
    return True
