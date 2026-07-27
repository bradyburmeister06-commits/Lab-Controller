# Arduino collection

The serial line format the firmware must emit, and how the collector keeps two
Arduinos running independently. Written against the Stage 2 hardening pass.

Code: `app/services/arduino_protocol.py` (format + validation) and
`app/services/sensor_service.py` (readers, reconnection, persistence).

## 1. The serial line format

**One reading per line, newline-terminated.** The collector reads exactly one
complete line at a time and parses it in isolation; nothing is carried between
lines, so a corrupt line can never poison the next one.

The canonical form is comma-separated `key=value` pairs:

```text
chamber=chamber-a,temp=22.41,rh=48.10,uptime=930112,fw=1.4.2,actuator=on
```

| Key | Required | Meaning | Accepted aliases |
| --- | --- | --- | --- |
| `temp` | **yes** | Temperature, in whatever unit the firmware reports (see §2) | `temperature`, `temp_c`, `temperature_c` |
| `rh` | **yes** | Relative humidity, percent | `humidity`, `relative_humidity`, `humidity_percent` |
| `chamber` | no | Chamber identifier this board is wired to | `chamber_id`, `id` |
| `uptime` | no | Milliseconds since boot (`millis()`) | `uptime_ms`, `millis` |
| `fw` | no | Firmware version string | `firmware`, `firmware_version`, `version` |
| `actuator` | no | Actuator/output state, lower-cased on ingest | `actuator_status`, `relay`, `output` |

Details:

- Separators may be `,` or `;`; `=` or `:` both work between key and value.
- Keys are case-insensitive. Unknown keys are **ignored**, so firmware can add
  fields (`dewpoint`, `co2`) without a collector change.
- A single-line JSON object with the same keys is also accepted, because older
  boards in the lab still emit it:
  `{"chamber":"chamber-a","temp":22.41,"rh":48.10,"fw":"1.4.2"}`

Minimal firmware loop:

```cpp
void loop() {
  Serial.print("chamber=chamber-a,temp=");
  Serial.print(readTemperature());
  Serial.print(",rh=");
  Serial.print(readHumidity());
  Serial.print(",uptime=");
  Serial.print(millis());
  Serial.println(",fw=1.4.2");
  delay(10000);
}
```

## 2. Validation: reject vs. flag

Two tiers, because "impossible" and "unlikely" need different handling.

**Rejected** (raises `SensorLineError`, nothing is stored, the line is logged at
debug with its original text):

- Empty line, or no `key=value` fields at all.
- Missing `temp` or `rh` — an incomplete record is never partially stored.
- A non-numeric, `NaN` or infinite `temp`/`rh`; a non-integer or negative `uptime`.
- Malformed JSON, or a token that is not `key=value`.
- Relative humidity outside **0–100%**.
- Temperature outside **-40 to 185** — beyond the range of any sensor we support.
- A `chamber` value that is not a valid identifier (`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`).
- A `chamber` that does not match the configured chamber for that port (§4).

**Flagged** (stored, with `quality_status` set):

| `quality_status` | Condition |
| --- | --- |
| `ok` | Normal |
| `suspect_temperature` | Inside the hard range but outside -10 to 140 |
| `suspect_humidity` | Exactly 0% or 100% — the classic rail-pinned DHT wiring fault |

Flagged readings are kept deliberately: discarding them would hide a failing
sensor. They are logged at warning level as they arrive.

> **Unit caveat.** `temp` is stored unit-agnostically, matching the existing
> `sensor_readings.temperature` column, and the ranges above are wide enough to
> accept either Celsius or Fahrenheit. The simulator emits Fahrenheit-looking
> values (68–74). Settling on one unit end to end is a Stage 3 item.

## 3. The internal reading object

`SensorReadingRecord` (`arduino_protocol.py`) is the single shape used by both
the simulator and real hardware, so nothing downstream can tell them apart:

| Field | Notes |
| --- | --- |
| `local_record_id` | 32-char uuid4 hex, generated per reading |
| `collector_id` | This collector's id (`COLLECTOR_ID`) |
| `chamber_id` | From the line, else the configured chamber, else `None` |
| `sensor_id` | Configured device name (`ARDUINO_1_NAME`) |
| `timestamp_utc` | Timezone-aware UTC, set at parse time |
| `temperature` | Validated float |
| `humidity_percent` | Validated float |
| `firmware_version`, `uptime_ms`, `actuator_status` | Optional |
| `quality_status` | `ok` / `suspect_temperature` / `suspect_humidity` |
| `raw_line` | The original stripped line, persisted as `raw_payload` |

The record is frozen. `sensor_service.save_record` re-runs the range checks
before writing, so a hand-built record cannot bypass validation on its way into
the database.

Two fields are not yet persisted: the `sensor_readings` table has no column for
`local_record_id` or `quality_status`, so both live in memory and in the logs
only. Adding them is a schema change and belongs with the Stage 3 sync queue.

## 4. Two Arduinos, two COM ports

`SensorIngestionManager` starts **one thread per configured device**
(`sensor-<name>`), and each thread owns its port end to end. There is no shared
serial state, no shared lock, and no shared failure path — the isolation is
structural, not defensive.

Per-device lifecycle (`_run_serial_reader`), which never raises:

1. Open the port. A missing port, a locked port (`Access is denied` on Windows
   when another program holds the COM port), or a bad cable all land in the same
   handler: record the error, back off, retry.
2. On success mark connected and pump lines until the port dies or shutdown.
3. On any read failure record the error, close the port, back off, reopen.

**Reconnect backoff** is linear in the number of consecutive failures —
`SENSOR_RECONNECT_DELAY_SECONDS` × failures, capped at 30 s — so an Arduino
that is simply not plugged in costs one open attempt every 30 s rather than
spinning a core. A successful open resets the counter.

`readline()` returning `b""` is a read timeout with the port still open, not a
disconnect, and is skipped without touching connection state.

**Reset and boot messages.** An Arduino re-prints its banner on every reset, and
a reset mid-line leaves framing garbage. Lines like `Arduino ready`,
`DHT22 sensor init`, `System reset` and all-garbage lines are classified as
`ArduinoNoiseLine`, logged at debug, and are not counted as malformed.

**Per-device state** is exposed by `manager.status()`, one dict per Arduino:
`connected`, `last_error`, `last_error_at`, `connect_failures`,
`malformed_lines`, and the last valid reading (timestamp, temperature,
humidity, quality, firmware). It is not yet surfaced over HTTP.

**Shutdown.** `stop()` sets the stop event and joins every reader thread; each
one closes its port in a `finally`, so no handle is leaked on restart.

## 5. Configuration

```text
SENSOR_SIMULATOR=false
ARDUINO_1_PORT=COM3          # /dev/ttyACM0 on Linux
ARDUINO_1_NAME=arduino-1
ARDUINO_1_CHAMBER_ID=chamber-a
ARDUINO_2_PORT=COM4          # /dev/ttyACM1 on Linux
ARDUINO_2_NAME=arduino-2
ARDUINO_2_CHAMBER_ID=chamber-b
ARDUINO_BAUDRATE=9600
SENSOR_READ_TIMEOUT_SECONDS=2
SENSOR_RECONNECT_DELAY_SECONDS=2
```

`ARDUINO_*_CHAMBER_ID` is optional but recommended. When set, a `chamber` on the
wire that disagrees is rejected rather than stored under the wrong name — which
is what makes a **swapped COM port configuration** fail loudly. Windows assigns
COM numbers by enumeration order, so the two boards can trade ports across a
reboot; without this check the swap is invisible in the data.

## 6. Tests

- `tests/test_sensor_parsing.py` — format, optional fields, extra fields,
  incomplete records, invalid numbers, out-of-range RH and temperature, chamber
  validation, reset messages.
- `tests/test_arduino_readers.py` — a scripted fake serial port drives two
  readers through independent operation, one board failing, a mid-stream drop,
  both boards down, reconnection, COM access errors and backoff, swapped ports,
  and port closure on shutdown.

No test touches real hardware; `serial_factory` is injected.
