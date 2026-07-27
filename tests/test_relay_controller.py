"""Stage 4: relay controller interface, MCC driver, and configuration guards."""
from __future__ import annotations

import sys
import types

import pytest

from app.config import Settings
from app.services.relay_controller import (
    MccUsb1208FsPlusController,
    MockRelayController,
    RelayConfigError,
    RelayConnectionError,
    RelayController,
    build_relay_controller,
    safe_all_off,
)


BITS = {"relay-1": 0, "relay-2": 1, "relay-3": 2}


class FlakyController(MockRelayController):
    """Mock whose port writes can be made to fail on demand."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail = False
        self.writes: list[int] = []

    def _write_byte(self, value: int) -> None:
        if self.fail:
            raise OSError("simulated USB detach")
        self.writes.append(value)


def test_controller_exposes_the_stage_4_interface():
    ctrl = MockRelayController(BITS)
    for name in ("initialize", "turn_on", "turn_off", "all_off", "get_states", "health"):
        assert callable(getattr(ctrl, name)), name


def test_turn_on_off_tracks_states_independently():
    ctrl = MockRelayController(BITS)
    ctrl.initialize()
    ctrl.turn_on("relay-1")
    ctrl.turn_on("relay-3")
    assert ctrl.get_states() == {"relay-1": True, "relay-2": False, "relay-3": True}
    ctrl.turn_off("relay-1")
    assert ctrl.get_states() == {"relay-1": False, "relay-2": False, "relay-3": True}


def test_all_off_clears_every_relay():
    ctrl = MockRelayController(BITS)
    ctrl.initialize()
    ctrl.turn_on("relay-1")
    ctrl.turn_on("relay-2")
    ctrl.all_off()
    assert ctrl.get_states() == {"relay-1": False, "relay-2": False, "relay-3": False}


def test_all_off_only_touches_mapped_bits_when_active_low():
    """An active-low board must not have unmapped port lines driven high."""
    ctrl = FlakyController({"relay-1": 0, "relay-2": 1}, active_high=False)
    ctrl.initialize()
    assert ctrl.latch == 0b011
    assert ctrl.get_states() == {"relay-1": False, "relay-2": False}


def test_initialize_forces_all_off():
    ctrl = MockRelayController(BITS)
    ctrl.turn_on("relay-2")
    ctrl.initialize()
    assert ctrl.get_states()["relay-2"] is False
    assert ctrl.initialized is True


def test_turn_on_unknown_relay_raises():
    ctrl = MockRelayController(BITS)
    with pytest.raises(RelayConfigError):
        ctrl.turn_on("relay-99")


def test_write_failure_raises_relay_connection_error():
    ctrl = FlakyController(BITS)
    ctrl.initialize()
    ctrl.fail = True
    with pytest.raises(RelayConnectionError):
        ctrl.turn_on("relay-1")
    with pytest.raises(RelayConnectionError):
        ctrl.all_off()


def test_set_state_reports_write_failure_instead_of_raising():
    """The audited write path must record a failure, not abort the request."""
    ctrl = FlakyController(BITS)
    ctrl.initialize()
    ctrl.fail = True
    result = ctrl.set_state("relay-1", True)
    assert result.success is False
    assert "failed" in result.message.lower()


def test_duplicate_bit_assignment_is_rejected():
    with pytest.raises(RelayConfigError):
        MockRelayController({"relay-1": 0, "relay-2": 0})


def test_out_of_range_bit_is_rejected():
    with pytest.raises(RelayConfigError):
        MockRelayController({"relay-1": 9})


def test_health_reports_state_and_initialization():
    ctrl = MockRelayController(BITS)
    ctrl.initialize()
    ctrl.turn_on("relay-2")
    health = ctrl.health()
    assert health["initialized"] is True
    assert health["any_on"] is True
    assert health["states"]["relay-2"] is True
    assert health["bit_map"] == BITS


def test_safe_all_off_never_raises():
    ctrl = FlakyController(BITS)
    ctrl.initialize()
    ctrl.fail = True
    assert safe_all_off(ctrl, "test") is False
    assert safe_all_off(None, "test") is False


def test_build_relay_controller_defaults_to_mock_and_does_not_import_mcculw():
    settings = Settings(_env_file=None)
    assert isinstance(build_relay_controller(settings), MockRelayController)
    assert "mcculw" not in sys.modules


def test_build_relay_controller_selects_mcc_without_importing_the_driver():
    settings = Settings(_env_file=None, relay_controller="mcc_usb1208fs_plus")
    ctrl = build_relay_controller(settings)
    assert isinstance(ctrl, MccUsb1208FsPlusController)
    # Construction must stay import-free; only initialize() touches mcculw.
    assert "mcculw" not in sys.modules


def _install_fake_mcculw(monkeypatch, *, config_error: Exception | None = None) -> dict:
    """Stand in for the Windows-only driver so the MCC path is testable on Linux."""
    calls: dict = {"config": [], "out": []}

    ul = types.SimpleNamespace(
        d_config_port=lambda board, port, direction: (
            (_ for _ in ()).throw(config_error)
            if config_error
            else calls["config"].append((board, port, direction))
        ),
        d_out=lambda board, port, value: calls["out"].append((board, port, value)),
    )
    enums = types.ModuleType("mcculw.enums")
    enums.DigitalIODirection = types.SimpleNamespace(OUT="OUT")
    enums.DigitalPortType = types.SimpleNamespace(FIRSTPORTA="A", FIRSTPORTB="B")
    root = types.ModuleType("mcculw")
    root.ul = ul

    monkeypatch.setitem(sys.modules, "mcculw", root)
    monkeypatch.setitem(sys.modules, "mcculw.enums", enums)
    return calls


def test_mcc_initialize_configures_port_and_forces_all_off(monkeypatch):
    calls = _install_fake_mcculw(monkeypatch)
    ctrl = MccUsb1208FsPlusController(BITS, board_num=3, digital_port="FIRSTPORTB")
    ctrl.initialize()

    assert calls["config"] == [(3, "B", "OUT")]
    assert calls["out"] == [(3, "B", 0)]
    assert ctrl.initialized is True
    assert ctrl.get_states() == {"relay-1": False, "relay-2": False, "relay-3": False}


def test_mcc_turn_on_writes_masked_byte(monkeypatch):
    calls = _install_fake_mcculw(monkeypatch)
    ctrl = MccUsb1208FsPlusController(BITS)
    ctrl.initialize()
    ctrl.turn_on("relay-3")
    assert calls["out"][-1] == (0, "B", 0b100)
    ctrl.all_off()
    assert calls["out"][-1] == (0, "B", 0)


def test_mcc_unknown_port_raises_config_error(monkeypatch):
    _install_fake_mcculw(monkeypatch)
    ctrl = MccUsb1208FsPlusController(BITS, digital_port="NOSUCHPORT")
    with pytest.raises(RelayConfigError):
        ctrl.initialize()


def test_mcc_config_failure_raises_connection_error(monkeypatch):
    _install_fake_mcculw(monkeypatch, config_error=OSError("board not found"))
    ctrl = MccUsb1208FsPlusController(BITS)
    with pytest.raises(RelayConnectionError) as excinfo:
        ctrl.initialize()
    assert "board not found" in str(excinfo.value)


def test_mcc_write_before_initialize_raises():
    ctrl = MccUsb1208FsPlusController(BITS)
    with pytest.raises(RelayConnectionError):
        ctrl.turn_on("relay-1")


def test_relay_controller_base_write_is_abstract():
    ctrl = RelayController(BITS)
    with pytest.raises(NotImplementedError):
        ctrl._write_byte(0)
