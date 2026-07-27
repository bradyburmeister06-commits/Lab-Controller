# Relay safety and local scheduling

Stage 4. How relays are driven, why an energised relay always gets turned off,
and how the collector keeps running its duty cycles when the hub is gone.

The rule this whole document exists to protect: **a relay that was turned on is
always turned off again** — on success, on exception, on cancellation, on
shutdown, and on the next startup.

## 1. Hardware layout: one MCC board, three SSRs

One Measurement Computing USB-1208FS-Plus drives three solid-state relays from
three bits of a single digital port:

| Relay | Default bit | Setting |
| --- | --- | --- |
| `relay-1` | 0 | `RELAY_1_BIT` |
| `relay-2` | 1 | `RELAY_2_BIT` |
| `relay-3` | 2 | `RELAY_3_BIT` |

- The port is selected with `MCC_DIGITAL_PORT` (default `FIRSTPORTB`) on board
  `MCC_BOARD_NUM` (default `0`). Port B lines are the documented high-current
  (24 mA) outputs on this device.
- `RELAY_ACTIVE_HIGH=false` inverts the electrical sense for boards whose inputs
  pull low to energise.
- The three bits must be **distinct**. `Settings` rejects a duplicate mapping at
  startup, because two relays sharing a bit would switch together.
- USB-1208FS-Plus DIO lines are TTL outputs. Do not drive relay coils directly —
  use an opto-isolated SSR board within the DIO output limits.

Because all three relays live on one port, every write is read-modify-write on a
cached output byte (the *latch*). `RelayController` holds the latch behind a
lock and masks single bits, so flipping one relay never disturbs the others.

## 2. The controller interface

`app/services/relay_controller.py`. One interface, three implementations
(`RelayController` base, `MockRelayController`, `MccUsb1208FsPlusController`).

| Method | Behaviour |
| --- | --- |
| `initialize()` | Configure the port for output, then force all relays off. Idempotent. |
| `turn_on(relay_id)` | Energise one relay. Raises `RelayError` on failure. |
| `turn_off(relay_id)` | De-energise one relay. Raises `RelayError` on failure. |
| `all_off()` | De-energise every mapped relay in one port write. |
| `get_states()` | `{relay_id: bool}` from the cached latch. |
| `health()` | Controller class, `initialized`, `active_high`, latch, bit map, states, `any_on`. |
| `set_state(relay_id, on)` | Returns a `RelayResult` instead of raising. |

Two call styles exist on purpose. `turn_on`/`turn_off`/`all_off` **raise**, so a
failed write on the fail-safe path can never be mistaken for a relay that is
actually off. `set_state` **returns a result**, because the audited write path
(`relay_service.apply_state`) has to record a failed write as a `RelayEvent`
rather than abort the request.

Errors are typed: `RelayConfigError` (unknown relay, bad bit, unknown port) and
`RelayConnectionError` (board unreachable, write failed), both under `RelayError`.

### Keeping `mcculw` off Linux

`mcculw` is Windows-only. It is imported **inside**
`MccUsb1208FsPlusController.initialize()`, never at module scope, so:

- hub and mock/simulator processes never load the driver, even though they
  import `relay_controller`;
- constructing the MCC controller is import-free — only `initialize()` touches
  the driver;
- on Linux `initialize()` raises `RelayConnectionError`, which `app/main.py`
  logs as a warning so startup continues with relay writes failing loudly.

`scripts/verify_modes.py` asserts `"mcculw" not in sys.modules` after booting
every mode. `tests/test_relay_controller.py` injects a fake `mcculw` module to
exercise the real MCC code path (port config, masked writes, error mapping) on
Linux.

## 3. Fail-safe activation

`app/services/relay_activation.py`. Every duration-bounded activation goes
through `RelayActivator.activate()`, which follows exactly this shape:

```python
controller.turn_on(relay_id)
try:
    await asyncio.sleep(duration_seconds)
finally:
    controller.turn_off(relay_id)
```

Guarantees, in the order they are enforced:

1. **Duration validation.** Zero, negative, and `None` are rejected
   (`InvalidDurationError`). So is anything above
   `RELAY_MAX_ACTIVATION_SECONDS` (default 300 s). Nothing is written to the
   hardware before this check passes.
2. **Relay validation.** Unknown relay → `KeyError`. A relay with
   `Relay.enabled = false` → `RelayDisabledError`.
3. **No overlap.** One `asyncio.Lock` per relay. A second activation for the
   same relay is **rejected** (`RelayBusyError`), not queued — queuing would
   silently extend total energised time. Different relays run concurrently.
4. **Always off.** The `finally` block runs on normal completion, on any
   exception, and on `asyncio.CancelledError` (which is re-raised after the
   relay is safe).
5. **Escalation.** If `turn_on` or `turn_off` itself fails, the failure is
   recorded and an emergency `all_off()` is attempted.

### Audit trail

Every step writes a `RelayEvent` to the **local** database before any network
call, so the history survives an offline hub and is later shipped by the Stage 3
sync queue (`synced_at IS NULL`).

| `action` | When |
| --- | --- |
| `activation_start` | Relay energised |
| `activation_end` | Relay de-energised (completed, cancelled, or errored) |
| `activation_failed` | `turn_on` failed; relay never came on |
| `deactivation_failed` | `turn_off` failed — relay state is **unknown**, `all_off` attempted |
| `all_off` | Manual or lifecycle all-off, one row per relay |
| `schedule` | Scheduler duty-cycle transition |
| `schedule_recovered` | Missed cycles skipped after a restart |

Recording is best-effort and never raises: losing a log line is better than
losing the relay.

### Operator endpoints

Both require admin basic auth (`app/auth.py::require_admin`) and act on the
hardware **this process** owns, so they keep working with the hub unreachable.

```bash
# Timed activation, guaranteed to turn off
curl -u admin:PASSWORD -X POST http://localhost:8000/api/relays/relay-1/activate \
     -H 'Content-Type: application/json' -d '{"duration_seconds": 30}'

# Panic button: de-energise everything now
curl -u admin:PASSWORD -X POST http://localhost:8000/api/relays/all-off
```

`POST /relays/{id}/activate` returns 422 for a bad duration, 404 for an unknown
relay, 409 for a disabled relay or an overlapping activation, and 503 when the
process owns no relay hardware (hub mode).

`GET /api/health` reports `relay_controller_initialized`, `relay_states`,
`relay_max_activation_seconds`, and `active_relay_activations`.

## 4. Local scheduling

`app/services/relay_scheduler.py`. One `RelayScheduler` per hardware-owning
process, bound to one `machine_key`. Hub mode runs none.

**Scheduling is local.** The hub is a source of schedule *updates*, never a
dependency of schedule *execution*. Schedules live in the collector's own
SQLite `relay_schedules` table, keyed `(machine_key, relay_id)`, each with its
own `enabled`, `on_duration_seconds`, `off_duration_seconds`, `current_phase`
and `next_run_at`. A collector whose hub or Tailscale link is down keeps cycling
and keeps recording relay events; the backlog ships on reconnect.

### Startup: `load_schedules()`

Runs before the tick loop starts, and is the recovery path after a crash:

1. `all_off()` — the process may have died mid-cycle with a relay energised.
2. Clear `Relay.is_on` for anything the database still thinks is on.
3. Clamp every stored duration into `[1, RELAY_MAX_ACTIVATION_SECONDS]`.
4. For each enabled schedule whose relay is also enabled: reset
   `current_phase = "off"` and `next_run_at = now`, so the cycle restarts
   cleanly from boot.
5. **Skip missed events.** Cycles that elapsed during the outage are counted and
   discarded, with one `schedule_recovered` event recording how many. Replaying
   a day of duty cycles at once is precisely the failure this guards against.
6. Disabled schedules get `next_run_at = None`.

### Ticking

A 1 s APScheduler job (`relay-scheduler-tick`, `max_instances=1`,
`coalesce=True`, UTC timezone) selects this machine's enabled schedules whose
`next_run_at` has elapsed and flips each one phase.

- **No overlapping runs.** `_advance` takes a non-blocking per-relay
  `threading.Lock`; a re-entrant call returns immediately rather than stacking a
  second transition onto the same relay.
- **No drift, no chain-firing.** The next transition is anchored to the
  *scheduled* time (`due + duration`) so the cycle does not drift by the tick
  interval each round. If that anchor is already in the past — i.e. we are more
  than one phase behind — it falls back to `now + duration`, so one overdue row
  produces one flip, not one flip per elapsed period.
- **Failure is safe.** If the transition raises, `all_off()` is attempted before
  the error propagates to the tick's logging guard.

### One scheduler per machine

`RelayScheduler.start()` registers its `machine_key` in a process-wide table. A
second scheduler for the same key raises `DuplicateSchedulerError` — two
schedulers would double-fire every cycle onto the same physical relay. The slot
is released by `stop()`. `start()` is also idempotent on the same instance.

### Timezones

All stored timestamps are **naive UTC** (SQLite does not preserve tzinfo
reliably). Arithmetic and comparisons use timezone-aware values via
`app/db/models.py`: `aware_utcnow()`, `as_utc()` to attach UTC when reading, and
`to_naive_utc()` before storing. The APScheduler instance is pinned to UTC.
Durations are therefore wall-clock independent: a cycle crossing a
daylight-saving boundary advances by exactly its configured duration, with no
lost or duplicated hour. `SCHEDULER_TIMEZONE` remains a display/`MachineScheduler`
setting and does not affect relay duty cycles.

### Validating schedule updates

- API (`PATCH /relays/{id}/schedule`): `on_duration_seconds` above
  `RELAY_MAX_ACTIVATION_SECONDS` is rejected with 422. The schema already bounds
  both durations to 1–86400 s.
- Hub-pushed schedules (`CollectorAgent._apply_schedules`): each duration is
  clamped into `[1, RELAY_MAX_ACTIVATION_SECONDS]` by `_sanitize_duration`. The
  hub is trusted but not infallible, and a malformed row must not be able to
  hold a relay on past the local cap.
- `RelayScheduler.validate_schedule_update()` exposes the same rules for callers
  that want to check before writing.

## 5. Startup and shutdown order

`app/main.py` splits the lifespan into `_startup()` and `_shutdown()`.

Startup, in hardware-safe order:

1. `init_db()` and seed defaults.
2. `relay_controller.initialize()` (warns and continues if the MCC driver is
   absent).
3. `safe_all_off()` — every relay off before anything that could fire one runs.
4. `relay_scheduler.load_schedules()` — recovery, which also forces a safe state.
5. Publish handles on `app.state` (including `relay_activator`).
6. Start the Arduino readers.
7. Start the machine scheduler and relay scheduler.
8. Start the collector sync/heartbeat agent.

If any step raises, `safe_all_off()` runs before the exception propagates, so a
partial startup cannot leave a relay energised.

Shutdown, in reverse-risk order: stop the schedulers first (no new transitions),
then `all_off()`, then stop the sensor readers and the collector agent. `stop()`
is idempotent on every service, and `safe_all_off()` never raises.

## 6. Tests

| File | Covers |
| --- | --- |
| `tests/test_relay_controller.py` | Interface, bit masking, active-low, config guards, error typing, fake-`mcculw` MCC driver path |
| `tests/test_relay_failsafe.py` | Duration validation, exception/cancellation during activation, overlap rejection, `turn_on`/`turn_off` failure escalation, manual all-off |
| `tests/test_relay_scheduler_offline.py` | Cycling with no hub, local event recording, restart recovery, missed-event skipping, duration clamping, duplicate schedulers, UTC/DST behaviour |
| `tests/test_relay_lifecycle.py` | Startup all-off, shutdown all-off, failed-startup all-off, health fields, admin `activate` and `all-off` endpoints |
| `tests/test_config_modes.py` | Duplicate bit rejection, max-activation bounds, MCC port normalisation, hub-mode inertness |

```bash
python -m pytest -q
python scripts/verify_modes.py   # also asserts relays are off at startup in every mode
```
