# Runtime flow and architecture

How a process starts, which services each `APP_MODE` runs, and which component
owns each piece of hardware. Written against the Stage 1 stabilization pass.

## 1. Startup sequence

Everything begins at `app/main.py`. Two distinct phases matter, and the split
between them is the single most important thing to understand about this app.

### Phase A — import time (module scope)

`app/main.py` builds its service singletons **when the module is imported**,
not when the app starts serving:

1. `settings = get_settings()` — a `functools.lru_cache`'d `Settings`
   (`app/config.py`), read from environment + `.env`.
2. `controller = build_controller(settings)` — the `MachineController`
   (mock / Wake-on-LAN / shell command). Always built, in every mode.
3. If `settings.runs_local_hardware` (i.e. mode is `collector` or `all_in_one`):
   `relay_controller`, `machine_scheduler`, `relay_scheduler`, and
   `sensor_manager` are constructed. Otherwise all four are `None`.
4. If `settings.app_mode == "collector"`: `collector_agent` is constructed.
   Otherwise `None`.

**Consequence:** `APP_MODE` cannot be changed after `app.main` is imported. Any
test or script that needs a different mode must use a fresh interpreter — this
is why `scripts/verify_modes.py` spawns a subprocess per mode.

Importing `app.db.session` also resolves `DATABASE_URL` at import time and
creates the SQLite parent directory if needed.

### Phase B — lifespan (`@asynccontextmanager lifespan`)

Runs on app startup, after import:

1. `init_db()` — `create_all` plus the hand-rolled additive migrations in
   `app/db/init_db.py` (adds missing columns; rewrites `relay_schedules` if it
   still has the legacy single-column primary key).
2. Seed defaults in one session: `ensure_default_machine`,
   `ensure_default_relays`. Then, **only if `runs_local_hardware`**,
   `ensure_default_relay_schedules(machine_key=collector_id)` and
   `ensure_default_collector`. A pure hub starts with an empty machine registry
   and fills it as collectors register.
3. `relay_controller.initialize()` — wrapped in try/except. On Linux/dev the
   MCC path raises (no `mcculw`) and is logged as a warning; startup continues.
4. Publish singletons onto `app.state` (`machine_scheduler`, `sensor_manager`,
   `relay_controller`, `relay_scheduler`, `collector_agent`). Routes read
   hardware handles from `app.state`, never from module globals.
5. Start each non-`None` service exactly once, in order: machine scheduler,
   relay scheduler, sensor manager, collector agent.
6. On shutdown, stop them in reverse order.

## 2. What runs in each mode

`app/config.py` derives three role flags from `app_mode`; these are the only
gate on hardware:

| Property             | `all_in_one` | `hub` | `collector` |
| -------------------- | ------------ | ----- | ----------- |
| `is_hub`             | yes          | yes   | no          |
| `is_collector`       | yes          | no    | yes         |
| `runs_local_hardware`| yes          | no    | yes         |

Resulting services per process:

| Service | Kind | `all_in_one` | `hub` | `collector` |
| --- | --- | --- | --- | --- |
| FastAPI app + routes | asyncio | yes | yes | yes |
| `MachineController` | in-process object | yes | yes | yes |
| `MachineScheduler` | APScheduler `BackgroundScheduler` | yes | — | yes |
| `RelayScheduler` | APScheduler `BackgroundScheduler` | yes | — | yes |
| `SensorIngestionManager` | 1 `threading.Thread` per Arduino | yes | — | yes |
| `RelayController` | in-process object | yes | — | yes |
| `CollectorAgent` | 1 `threading.Thread` | — | — | yes |

Notes:

- **Hub** serves dashboards and the ingest/command API only. It holds no relay
  controller, so admin relay writes are enqueued as `CollectorCommand` rows
  rather than executed (see §4).
- **Collector** still serves the full HTTP app locally (useful for
  `status-collector.bat` health probes) but its authority for schedules is the
  hub.
- **`all_in_one`** is hub + collector in one process, with no HTTP hop.

`scripts/verify_modes.py` asserts this table at runtime, and
`tests/test_config_modes.py::test_all_modes_start_and_serve_routes` runs it in CI.

## 3. Background tasks: one owner per resource

### Schedulers

There are **two** APScheduler `BackgroundScheduler` instances in a
hardware-owning process, with disjoint responsibilities. This is intentional,
not duplication:

| Scheduler | Job id | Interval | Responsibility |
| --- | --- | --- | --- |
| `MachineScheduler` (`services/scheduler.py`) | `machine-scheduler-tick` | 5 s | Activates `Machine` rows whose `next_run_at` has elapsed |
| `RelayScheduler` (`services/relay_scheduler.py`) | `relay-scheduler-tick` | 1 s | Advances per-relay ON/OFF duty cycles |

Each scheduler owns exactly one job (`replace_existing=True`, and
`max_instances=1` on the relay tick so a slow tick cannot overlap itself).
`hub` mode runs **zero** schedulers.

### Start-once guarantees

`lifespan` calls each `start()` once per process, and each `start()` is
additionally idempotent so a double-start cannot duplicate work:

- `MachineScheduler.start` / `RelayScheduler.start` — return early if
  `self.scheduler.running`.
- `CollectorAgent.start` — returns early if its thread is alive.
- `SensorIngestionManager.start` — returns early if threads already exist.
  (This guard was added in Stage 1; without it a second `start()` would attach a
  second reader thread to the same serial port and double-record every reading.)

Covered by `tests/test_service_lifecycle.py`.

### One reader per Arduino

`SensorIngestionManager` spawns exactly one thread per `SensorDevice`, named
`sensor-<device name>`, and the device list is built once in `app/main.py` from
`ARDUINO_1_*` / `ARDUINO_2_*`. Nothing else in the codebase opens a serial port
— `import serial` appears only in `app/services/sensor_service.py`, and it is a
guarded optional import. In simulator mode (`SENSOR_SIMULATOR=true`, the
default) the same threads generate synthetic readings instead and no serial port
is touched.

### One writer per relay

Every relay state change in the process funnels through a single function,
`relay_service.apply_state`, which is the **only** caller of
`RelayController.set_state`. Its three call sites are:

- `app/api/routes.py` — operator actions (`/relays/{id}/set|on|off|toggle`)
- `app/services/relay_scheduler.py` — duty-cycle transitions
- `app/services/collector_agent.py` — commands pulled from the hub

`apply_state` writes the `Relay` row and appends a `RelayEvent` in one
transaction, so state and audit log cannot diverge. Below it, a single
`RelayController` instance per process holds the output byte (`_latch`) behind a
`threading.Lock` and applies bit masking, so flipping one relay never disturbs
another bit on the same MCC port. `RelayScheduler` adds a per-relay
`threading.Lock` on top, so concurrent duty-cycle advances for the same relay
are serialized.

## 4. Hub ↔ collector data flow

`CollectorAgent` (`services/collector_agent.py`) runs one loop thread on the
collector, using the local SQLite DB as a transient buffer:

- **Register** (once): `POST /api/collector/register`.
- **Push** (every `COLLECTOR_PUSH_INTERVAL_SECONDS`, default 10 s):
  `POST /api/collector/heartbeat`, then new `SensorReading` rows to
  `/api/collector/sensor-readings` and new `RelayEvent` rows plus a full
  `relay_states` snapshot to `/api/collector/relay-events`. Uploads are tracked
  by monotonic row-id watermarks (`_last_sensor_id`, `_last_relay_event_id`), so
  rows are never sent twice.
- **Poll** (every `COLLECTOR_POLL_INTERVAL_SECONDS`, default 5 s):
  `GET /api/collector/poll` returns relays, schedule rows scoped to this
  `collector_id`, and pending commands. Schedules are mirrored into the local
  DB and applied via `RelayScheduler.apply_schedule_change`; commands are
  executed through `apply_state` / `toggle_relay` and then acked via
  `/api/collector/command-ack`.

All collector endpoints require the `X-Collector-Token` header
(`app/auth.py::require_collector_token`). Failures back off exponentially
(capped at 60 s) and never kill the loop — a collector with an unreachable hub
keeps running its local schedule. This is verified by `scripts/verify_modes.py`,
which boots collector mode against an unroutable `HUB_BASE_URL`.

### Machine-key scoping

A collector must never execute another machine's duty cycle. Three independent
guards enforce this:

1. The hub filters `/api/collector/poll` schedules by `collector_id`.
2. `CollectorAgent._apply_schedules` re-checks each row's `machine_key` and
   skips foreign rows.
3. `RelayScheduler` is bound to one `machine_key` at construction and both its
   tick query and `apply_schedule_change` refuse to act on any other key.

Keys are validated against `MACHINE_KEY_RE` in `app/config.py` (1–64 chars,
lowercase alphanumeric plus `-`, `_`, `.`) at every registration entry point.

## 5. Known duplication and risks (deferred to later stages)

Stage 1 deliberately did not rewrite these. They are recorded here so the next
pass has a starting point.

1. **`Relay` rows are global, not machine-scoped.** `SensorReading`,
   `RelayEvent`, and `RelaySchedule` all carry a `machine_key`, but `Relay` is
   keyed on `id` alone. On a hub serving multiple collectors, all of them share
   three `relays` rows, and each collector's `relay_states` push overwrites the
   same `is_on` values. Per-machine relay state needs a schema change
   (`(machine_key, relay_id)` primary key) plus a data migration. **This is the
   highest-value Stage 2 item.**

2. **Terminology overlap: "machine".** Three different things use the word:
   - `Machine` (table `machines`) — the research apparatus that gets activated
     via Wake-on-LAN or a shell command.
   - `Collector` (table `collectors`) — a lab PC running collector mode.
   - `machine_key` — despite the name, this holds a **collector id**, never a
     `Machine.id`.

   Admin routes compound this by exposing collectors under
   `/api/admin/machines/{machine_key}`. Renaming is a wide, breaking change
   across the schema, API, and both dashboards, so it was left alone. A later
   stage should settle on one vocabulary (suggested: *collector* for the lab PC,
   *machine* only for the apparatus) and migrate deliberately.

3. **Single global `Machine`.** `dashboard_payload` resolves exactly one machine
   via `settings.default_machine_id`, so the dashboard is single-apparatus even
   though the collector registry is multi-machine.

4. **`CollectorAgent._push_once` does not filter by `machine_key`.** It selects
   every `SensorReading`/`RelayEvent` above its watermark. Correct in practice
   (a collector's DB only holds its own rows) but inconsistent with
   `_init_watermarks`, which does filter. Add the filter when relay rows become
   machine-scoped.

5. **Unused hub helpers.** `collector_hub.collector_is_stale` and
   `collector_hub.get_schedule` have no call sites. They are the documented
   counterparts of functions that *are* used, so they were kept rather than
   deleted; remove them if Stage 2 confirms they are not wanted.

6. **`relay_service.DEFAULT_RELAY_IDS` is unused** — `init_db.ensure_default_relays`
   hardcodes its own relay list. Collapse to one source of truth when relays
   become configurable.

7. **No `conftest.py`.** Tests import `app.main` at module scope, which starts
   real schedulers and simulator threads and writes to the real
   `DATABASE_URL`. A session-scoped fixture with a temp database would make the
   suite hermetic and faster.

## 6. Verifying a change

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q            # full suite
.venv/bin/python scripts/verify_modes.py # boot all three modes, mock hardware
```

`requirements-windows.txt` (`mcculw`) is **not** needed on Linux or in CI. It is
only required on a Windows collector driving real MCC hardware, and must be
installed after MCC InstaCal / Universal Library. Without it,
`RELAY_CONTROLLER=mcc_usb1208fs_plus` still starts — `initialize()` raises and
the failure is logged as a warning — but relay writes will fail.

To run a single mode manually:

```bash
APP_MODE=hub .venv/bin/python -m uvicorn app.main:app --port 8000
```

Dashboard routes resolve their static files relative to the `app` package, so
the server no longer has to be launched from the repository root.
