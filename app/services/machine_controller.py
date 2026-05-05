from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass

from app.config import Settings


@dataclass
class ControlResult:
    success: bool
    message: str


class MachineController:
    def turn_on(self, machine_id: str) -> ControlResult:
        raise NotImplementedError


class MockMachineController(MachineController):
    def turn_on(self, machine_id: str) -> ControlResult:
        return ControlResult(True, f"Mock activation recorded for {machine_id}. No hardware command was sent.")


class WakeOnLanController(MachineController):
    def __init__(self, mac_address: str) -> None:
        self.mac_address = mac_address

    def turn_on(self, machine_id: str) -> ControlResult:
        try:
            packet = self._build_magic_packet(self.mac_address)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, ("255.255.255.255", 9))
            return ControlResult(True, f"Wake-on-LAN packet sent for {machine_id}.")
        except Exception as exc:  # pragma: no cover - hardware/network dependent
            return ControlResult(False, f"Wake-on-LAN failed: {exc}")

    @staticmethod
    def _build_magic_packet(mac_address: str) -> bytes:
        cleaned = mac_address.replace(":", "").replace("-", "")
        if len(cleaned) != 12:
            raise ValueError("WOL_MAC_ADDRESS must contain 12 hexadecimal digits.")
        mac_bytes = bytes.fromhex(cleaned)
        return b"\xff" * 6 + mac_bytes * 16


class CommandController(MachineController):
    def __init__(self, command: str) -> None:
        self.command = command

    def turn_on(self, machine_id: str) -> ControlResult:
        try:
            completed = subprocess.run(
                self.command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode == 0:
                return ControlResult(True, f"Command activation succeeded for {machine_id}: {completed.stdout.strip()}")
            return ControlResult(False, f"Command activation failed: {completed.stderr.strip()}")
        except Exception as exc:  # pragma: no cover - command/environment dependent
            return ControlResult(False, f"Command activation error: {exc}")


def build_controller(settings: Settings) -> MachineController:
    if settings.machine_controller == "wol":
        if not settings.wol_mac_address:
            raise ValueError("MACHINE_CONTROLLER=wol requires WOL_MAC_ADDRESS.")
        return WakeOnLanController(settings.wol_mac_address)
    if settings.machine_controller == "command":
        if not settings.command_on:
            raise ValueError("MACHINE_CONTROLLER=command requires COMMAND_ON.")
        return CommandController(settings.command_on)
    return MockMachineController()
