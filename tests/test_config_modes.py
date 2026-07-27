"""APP_MODE validation, mode-derived hardware isolation, and startup smoke tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, is_valid_machine_key


REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's local .env from leaking into assertions.
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("mode", ["all_in_one", "hub", "collector"])
def test_valid_app_modes_are_accepted(mode):
    assert _settings(app_mode=mode).app_mode == mode


@pytest.mark.parametrize("mode", ["", "HUB", "all-in-one", "standalone", "collector "])
def test_invalid_app_mode_is_rejected(mode):
    with pytest.raises(ValidationError):
        _settings(app_mode=mode)


def test_default_app_mode_is_all_in_one():
    assert _settings().app_mode == "all_in_one"


@pytest.mark.parametrize(
    "mode,runs_local_hardware,is_hub,is_collector",
    [
        ("all_in_one", True, True, True),
        ("hub", False, True, False),
        ("collector", True, False, True),
    ],
)
def test_mode_derived_role_flags(mode, runs_local_hardware, is_hub, is_collector):
    """These flags are what app.main uses to decide whether to build hardware
    services, so they are the single gate for hardware isolation."""
    settings = _settings(app_mode=mode)
    assert settings.runs_local_hardware is runs_local_hardware
    assert settings.is_hub is is_hub
    assert settings.is_collector is is_collector


def test_hub_mode_never_runs_local_hardware():
    assert _settings(app_mode="hub", relay_controller="mcc_usb1208fs_plus").runs_local_hardware is False


@pytest.mark.parametrize("controller", ["mock", "mcc_usb1208fs_plus"])
def test_valid_relay_controllers_are_accepted(controller):
    assert _settings(relay_controller=controller).relay_controller == controller


def test_invalid_relay_controller_is_rejected():
    with pytest.raises(ValidationError):
        _settings(relay_controller="usb-relay")


def test_invalid_machine_controller_is_rejected():
    with pytest.raises(ValidationError):
        _settings(machine_controller="ssh")


@pytest.mark.parametrize("key", ["collector-1", "lab.a", "a", "lab_b2"])
def test_valid_machine_keys(key):
    assert is_valid_machine_key(key)


@pytest.mark.parametrize("key", ["", None, "Collector-1", "-lead", "has space", "a" * 65])
def test_invalid_machine_keys(key):
    assert not is_valid_machine_key(key)


def test_relay_bit_map_tracks_configured_bits():
    settings = _settings(relay_1_bit=3, relay_2_bit=4, relay_3_bit=5)
    assert settings.relay_bit_map == {"relay-1": 3, "relay-2": 4, "relay-3": 5}


@pytest.mark.parametrize("bit", [-1, 8])
def test_relay_bit_out_of_port_range_is_rejected(bit):
    with pytest.raises(ValidationError):
        _settings(relay_1_bit=bit)


def test_all_modes_start_and_serve_routes():
    """Boots app.main once per APP_MODE in a subprocess with mock hardware.

    app.main builds its service singletons at import time, so each mode needs
    its own interpreter. This also asserts that no mode imports the
    Windows-only mcculw driver.
    """
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_modes.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_arduino_chamber_id_disables_the_chamber_check(value):
    assert _settings(arduino_1_chamber_id=value).arduino_1_chamber_id is None


def test_arduino_chamber_id_is_kept_when_set():
    assert _settings(arduino_2_chamber_id=" chamber-b ").arduino_2_chamber_id == "chamber-b"
