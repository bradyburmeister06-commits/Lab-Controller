"""Startup smoke-check for every APP_MODE, using mock/simulator hardware.

Run from the repository root:

    python scripts/verify_modes.py

The parent process spawns one child per mode. Each child sets APP_MODE before
importing ``app.main`` (the module builds its service singletons at import
time, so the mode cannot be switched in-process) and then drives the app with
``TestClient``. Exits non-zero if any mode fails.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODES = ("all_in_one", "hub", "collector")
ADMIN_AUTH = ("admin", "change-me-now")

# Which service singletons app.main is expected to build in each mode.
EXPECTED_SERVICES = {
    "all_in_one": {
        "relay_controller": True,
        "machine_scheduler": True,
        "relay_scheduler": True,
        "sensor_manager": True,
        "collector_agent": False,
    },
    "hub": {
        "relay_controller": False,
        "machine_scheduler": False,
        "relay_scheduler": False,
        "sensor_manager": False,
        "collector_agent": False,
    },
    "collector": {
        "relay_controller": True,
        "machine_scheduler": True,
        "relay_scheduler": True,
        "sensor_manager": True,
        "collector_agent": True,
    },
}


def run_child(mode: str) -> dict:
    """Import app.main under ``mode`` and report health/route/service state."""
    from fastapi.testclient import TestClient

    import app.main as main_mod

    services = {
        name: getattr(main_mod, name) is not None for name in EXPECTED_SERVICES[mode]
    }

    with TestClient(main_mod.app) as client:
        health = client.get("/api/health")
        result = {
            "mode": mode,
            "health_status": health.status_code,
            "health": health.json() if health.status_code == 200 else None,
            "root_status": client.get("/").status_code,
            "public_status": client.get("/public").status_code,
            "admin_unauthenticated_status": client.get("/admin").status_code,
            "admin_authenticated_status": client.get("/admin", auth=ADMIN_AUTH).status_code,
            "services": services,
        }

    # Hardware isolation: neither hub nor a simulator/mock collector may pull in
    # the Windows-only MCC driver.
    result["mcculw_imported"] = "mcculw" in sys.modules
    return result


def check(result: dict) -> list[str]:
    mode = result["mode"]
    failures: list[str] = []

    def expect(label: str, actual, wanted) -> None:
        if actual != wanted:
            failures.append(f"[{mode}] {label}: expected {wanted!r}, got {actual!r}")

    expect("GET /api/health", result["health_status"], 200)
    expect("GET /", result["root_status"], 200)
    expect("GET /public", result["public_status"], 200)
    expect("GET /admin (no auth)", result["admin_unauthenticated_status"], 401)
    expect("GET /admin (auth)", result["admin_authenticated_status"], 200)
    expect("mcculw imported", result["mcculw_imported"], False)

    health = result["health"] or {}
    expect("health.status", health.get("status"), "ok")
    expect("health.database", health.get("database"), "ok")
    expect("health.app_mode", health.get("app_mode"), mode)

    # Stage 4: a hardware-owning process must come up with its relay controller
    # initialized and every relay de-energised. A hub reports neither.
    if EXPECTED_SERVICES[mode]["relay_controller"]:
        expect(
            "health.relay_controller_initialized",
            health.get("relay_controller_initialized"),
            True,
        )
        expect(
            "relays all off at startup",
            sorted(set((health.get("relay_states") or {}).values())),
            [False],
        )
        expect("health.active_relay_activations", health.get("active_relay_activations"), [])
    else:
        expect(
            "health.relay_controller_initialized",
            health.get("relay_controller_initialized"),
            None,
        )
        expect("health.relay_states", health.get("relay_states"), None)

    for name, wanted in EXPECTED_SERVICES[mode].items():
        expect(f"service {name} built", result["services"].get(name), wanted)

    return failures


def spawn(mode: str, workdir: Path) -> tuple[dict | None, str]:
    env = {
        **os.environ,
        "APP_MODE": mode,
        "PYTHONPATH": str(REPO_ROOT),
        "DATABASE_URL": f"sqlite:///{workdir / f'verify_{mode}.db'}",
        "SENSOR_SIMULATOR": "true",
        "RELAY_CONTROLLER": "mock",
        "MACHINE_CONTROLLER": "mock",
        # Unroutable: the collector agent must tolerate an unreachable hub.
        "HUB_BASE_URL": "http://127.0.0.1:9",
    }
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", mode],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :]), proc.stderr
    return None, proc.stdout + proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=MODES, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        print("RESULT " + json.dumps(run_child(args.child)))
        return 0

    all_failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for mode in MODES:
            result, stderr = spawn(mode, Path(tmp))
            if result is None:
                all_failures.append(f"[{mode}] child process produced no result:\n{stderr}")
                print(f"FAIL {mode}: startup crashed")
                continue
            failures = check(result)
            all_failures.extend(failures)
            print(f"{'FAIL' if failures else ' OK '} {mode}")

    if all_failures:
        print("\nFailures:")
        for failure in all_failures:
            print(f"  - {failure}")
        return 1
    print("\nAll modes started and served their routes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
