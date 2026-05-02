#!/usr/bin/env python3

import csv
import json
import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import serial  (#type:ignore)
from flask import Flask, jsonify, render_template

from uldaq import (  # type: ignore
    DaqDevice,
    DigitalDirection,
    DigitalPortType,
    InterfaceType,
    get_daq_device_inventory,
)

app = Flask(__name__)

BASE_DIR = Path("/app")
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
SENSOR_LOG = LOG_DIR / "sensor_data.csv"
RELAY_LOG = LOG_DIR / "relay_controller.log"

PORT = DigitalPortType.AUXPORT
RELAYS = {"SSR1": 0, "SSR2": 1, "SSR3": 2}

running = True
state_lock = threading.Lock()
daq_lock = threading.Lock()

controller: "Controller | None" = None

system_state: dict[str, Any] = {
    "controller": "STARTING",
    "arduino": "STARTING",
    "temp": None,
    "rh": None,
    "last_update": None,
    "relays": {
        name: {
            "state": "OFF",
            "last_on": None,
            "next_on": None,
            "turns_off_at": None,
        }
        for name in RELAYS
    },
}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s,%(levelname)s,%(message)s",
        handlers=[
            logging.FileHandler(RELAY_LOG),
            logging.StreamHandler(),
        ],
    )

    if not SENSOR_LOG.exists():
        with SENSOR_LOG.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "temp_c", "rh_percent"])


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r") as f:
        return json.load(f)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_time(ts: float | None) -> str:
    if ts is None:
        return "Never"
    return datetime.fromtimestamp(ts).strftime("%I:%M:%S %p")


def format_countdown(ts: float | None) -> str:
    if ts is None:
        return "N/A"

    remaining = int(ts - time.time())

    if remaining <= 0:
        return "Due now"

    hours = remaining // 3600
    minutes = (remaining % 3600) // 60
    seconds = remaining % 60

    if hours > 0:
        return f"{hours} hr {minutes} min {seconds} sec"
    if minutes > 0:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


class Controller:
    def __init__(self) -> None:
        self.dev = None
        self.dio = None

    def connect(self) -> None:
        devices = get_daq_device_inventory(InterfaceType.USB)

        if not devices:
            raise RuntimeError("No MCC USB DAQ device found.")

        self.dev = DaqDevice(devices[0])
        self.dev.connect()
        self.dio = self.dev.get_dio_device()

        logging.info("Connected to DAQ: %s", devices[0].product_name)

        for name, bit in RELAYS.items():
            self.dio.d_config_bit(PORT, bit, DigitalDirection.OUTPUT)
            self.set_relay(name, False)

        with state_lock:
            system_state["controller"] = "RUNNING"

        logging.info("All SSRs configured and forced OFF.")

    def set_relay(self, name: str, state: bool) -> None:
        if name not in RELAYS:
            raise ValueError(f"Unknown relay: {name}")

        if self.dio is None:
            raise RuntimeError("DAQ is not connected.")

        bit = RELAYS[name]
        value = 1 if state else 0

        with daq_lock:
            self.dio.d_bit_out(PORT, bit, value)

        with state_lock:
            system_state["relays"][name]["state"] = "ON" if state else "OFF"

        logging.info("%s,%s,bit=%s", name, "ON" if state else "OFF", bit)

    def all_off(self) -> None:
        for name in RELAYS:
            try:
                self.set_relay(name, False)
            except Exception as e:
                logging.error("Failed to turn %s OFF: %s", name, e)

    def disconnect(self) -> None:
        self.all_off()

        if self.dev is not None:
            self.dev.disconnect()
            self.dev.release()

        with state_lock:
            system_state["controller"] = "STOPPED"

        logging.info("DAQ disconnected.")


def relay_loop(name: str, cfg: dict[str, Any]) -> None:
    global running

    pulse_seconds = float(cfg["pulse_seconds"])
    interval_hours = float(cfg["interval_hours"])
    interval_seconds = interval_hours * 3600

    if pulse_seconds <= 0:
        logging.error("%s has invalid pulse_seconds.", name)
        return

    if interval_seconds <= pulse_seconds:
        logging.error("%s interval must be longer than pulse time.", name)
        return

    logging.info(
        "%s schedule started: %.2f second pulse every %.2f hours",
        name,
        pulse_seconds,
        interval_hours,
    )

    while running:
        cycle_start = time.time()
        turns_off_at = cycle_start + pulse_seconds
        next_on = cycle_start + interval_seconds

        with state_lock:
            system_state["relays"][name]["last_on"] = cycle_start
            system_state["relays"][name]["next_on"] = next_on
            system_state["relays"][name]["turns_off_at"] = turns_off_at

        try:
            assert controller is not None
            controller.set_relay(name, True)
            logging.info("%s pulse started", name)

            while running and time.time() < turns_off_at:
                time.sleep(0.2)

            controller.set_relay(name, False)
            logging.info("%s pulse ended", name)

        except Exception as e:
            logging.error("%s relay loop error: %s", name, e)

        finally:
            with state_lock:
                system_state["relays"][name]["turns_off_at"] = None

            try:
                assert controller is not None
                controller.set_relay(name, False)
            except Exception:
                pass

        while running and time.time() < next_on:
            time.sleep(1)


def arduino_loop(cfg: dict[str, Any]) -> None:
    port = cfg.get("port", "/dev/ttyACM0")
    baudrate = int(cfg.get("baudrate", 9600))

    logging.info("Starting Arduino logger on %s at %s baud", port, baudrate)

    while running:
        try:
            with serial.Serial(port, baudrate, timeout=2) as ser:
                logging.info("Arduino connected.")

                with state_lock:
                    system_state["arduino"] = "RUNNING"

                while running:
                    raw = ser.readline().decode("utf-8", errors="ignore").strip()

                    if not raw:
                        continue

                    try:
                        temp_c_str, rh_str = raw.split(",", maxsplit=1)
                        temp_c = float(temp_c_str)
                        rh = float(rh_str)

                        timestamp = now_iso()

                        with SENSOR_LOG.open("a", newline="") as f:
                            writer = csv.writer(f)
                            writer.writerow([timestamp, temp_c, rh])

                        with state_lock:
                            system_state["temp"] = temp_c
                            system_state["rh"] = rh
                            system_state["last_update"] = time.time()

                        logging.info("SENSOR,temp_c=%.2f,rh=%.2f", temp_c, rh)

                    except ValueError:
                        logging.warning("Bad Arduino line ignored: %s", raw)

        except serial.SerialException as e:
            logging.error("Arduino serial error: %s", e)

            with state_lock:
                system_state["arduino"] = "ERROR"

            time.sleep(10)


def get_history(limit: int = 300) -> dict[str, list[Any]]:
    if not SENSOR_LOG.exists():
        return {"timestamps": [], "temps": [], "rhs": []}

    rows: list[dict[str, str]] = []

    with SENSOR_LOG.open("r") as f:
        reader = csv.DictReader(f)
        rows.extend(reader)

    rows = rows[-limit:]

    return {
        "timestamps": [r["timestamp"] for r in rows],
        "temps": [float(r["temp_c"]) for r in rows],
        "rhs": [float(r["rh_percent"]) for r in rows],
    }


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/status")
def status():
    with state_lock:
        snapshot = json.loads(json.dumps(system_state))

    relays = {}

    for name, data in snapshot["relays"].items():
        relays[name] = {
            "state": data["state"],
            "last_on": format_time(data["last_on"]),
            "next_on": format_time(data["next_on"]),
            "next_on_in": format_countdown(data["next_on"]),
            "turns_off_in": format_countdown(data["turns_off_at"])
            if data["state"] == "ON"
            else "N/A",
            "color": "blue" if data["state"] == "ON" else "gray",
        }

    return jsonify(
        {
            "system": {
                "controller": snapshot["controller"],
                "arduino": snapshot["arduino"],
                "server_time": datetime.now().strftime("%I:%M:%S %p"),
            },
            "current": {
                "temp_c": snapshot["temp"],
                "rh_percent": snapshot["rh"],
                "last_sensor_update": format_time(snapshot["last_update"]),
            },
            "relays": relays,
        }
    )


@app.route("/api/history")
def history():
    return jsonify(get_history())


@app.route("/api/test/<relay_name>", methods=["POST"])
def test_relay(relay_name: str):
    relay_name = relay_name.upper()

    if relay_name not in RELAYS:
        return jsonify({"ok": False, "error": "Unknown relay"}), 400

    if controller is None:
        return jsonify({"ok": False, "error": "Controller not ready"}), 500

    def test_pulse() -> None:
        try:
            logging.warning("Manual test started for %s", relay_name)
            controller.set_relay(relay_name, True)
            time.sleep(2)
            controller.set_relay(relay_name, False)
            logging.warning("Manual test finished for %s", relay_name)
        except Exception as e:
            logging.error("Manual test failed for %s: %s", relay_name, e)
            try:
                controller.set_relay(relay_name, False)
            except Exception:
                pass

    threading.Thread(target=test_pulse, daemon=True).start()

    return jsonify({"ok": True, "message": f"{relay_name} test started"})


def shutdown_handler(signum, frame) -> None:
    global running
    logging.warning("Shutdown received. Stopping safely...")
    running = False


def start_threads(config: dict[str, Any]) -> None:
    for relay_name, relay_cfg in config["relays"].items():
        if relay_name not in RELAYS:
            logging.warning("Ignoring unknown relay: %s", relay_name)
            continue

        threading.Thread(
            target=relay_loop,
            args=(relay_name, relay_cfg),
            daemon=True,
        ).start()

    threading.Thread(
        target=arduino_loop,
        args=(config["arduino"],),
        daemon=True,
    ).start()


def main() -> None:
    global controller

    setup_logging()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    config = load_config()

    try:
        controller = Controller()
        controller.connect()

        start_threads(config)

        logging.info("Starting dashboard on port 8080")
        app.run(host="0.0.0.0", port=8080, threaded=True)

    except Exception as e:
        logging.error("Fatal error: %s", e)

    finally:
        if controller is not None:
            controller.disconnect()
        logging.info("Stopped cleanly.")


if __name__ == "__main__":
    main()
