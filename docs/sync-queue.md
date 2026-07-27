# Sync queue: local-first storage and hub ingestion

Stage 3. How a reading taken in the lab reaches the hub, and why it survives a
network outage, a hub restart, and a lost HTTP response.

Before Stage 3 the collector shipped anything newer than an in-memory row-id
watermark. That watermark was lost on restart and advanced as soon as a request
returned, so a crash mid-push could skip records and a lost response could
duplicate them. The queue below replaces it.

## 1. The rule: save locally, then upload

Every record the collector generates is committed to local SQLite **before**
any upload is attempted:

- Sensor readings — `sensor_service.save_reading`, called by the Arduino reader
  threads.
- Relay events — `relay_service.apply_state` / `toggle_relay`, called by the
  scheduler, the API, and hub commands.

Both write inside a transaction that rolls back on failure, so a record is
either fully stored or not stored at all. Neither function contacts the hub. A
collector whose network dies one millisecond after a reading is taken has
already persisted that reading.

Records are **never deleted after synchronization**. `synced_at` is stamped and
the row stays as the collector's own history. Pruning is a separate retention
concern, deliberately out of scope here.

## 2. Sync columns

`sensor_readings` and `relay_events` each carry:

| Column | Meaning |
| --- | --- |
| `local_record_id` | Random hex id assigned once at insert. The dedup key. Never regenerated on retry. |
| `collector_id` | Which collector produced the row. Scopes both the queue read and the dedup key. |
| `created_at` | When the collector wrote the row. |
| `synced_at` | `NULL` = pending. Set only after the hub confirms. |
| `sync_attempts` | Incremented on each failed upload. |
| `last_sync_error` | Last failure reason, token-redacted, truncated to 500 chars. |

Two more tables:

- `collector_events` — local operational events (startup, port loss, sync
  failures) with the same sync columns, so an offline machine keeps its own
  operational history.
- `sync_state` — one row per `(collector_id, stream)` holding
  `last_success_at`, `last_attempt_at`, `consecutive_failures`,
  `pending_count`, and `synced_total`. Survives restarts.

Indexes: `ix_sensor_readings_unsynced` and `ix_relay_events_unsynced` on
`(collector_id, synced_at, id)` serve the queue read; `ix_relay_events_time`
and `ix_relay_schedules_next_run` serve history and scheduling.

## 3. Upload path

`CollectorAgent` splits into one method per responsibility, each callable
directly from a test without starting the thread:

`register_collector()`, `send_heartbeat()`, `poll_schedules()`,
`push_pending_readings()`, `push_pending_relay_events()`.

One push does:

1. Read up to `COLLECTOR_SYNC_BATCH_SIZE` rows where `synced_at IS NULL`,
   oldest id first. The cap bounds memory and request size no matter how long
   the collector was offline.
2. `POST` the batch. The hub replies with `accepted`, `duplicates`, and
   `rejected` id lists.
3. Stamp `synced_at` on `accepted + duplicates` — a duplicate is a success, the
   hub already has it. `rejected` records (bad range, unknown relay) are also
   stamped, because they are permanently unacceptable and would otherwise block
   the queue behind them forever.
4. Update `sync_state` and the agent's `last_sync_at` / pending counters, which
   `GET /api/health` exposes as `last_sync_at`, `pending_readings`, and
   `pending_relay_events`.

On failure the rows stay pending, `sync_attempts` is incremented,
`last_sync_error` is recorded, and the stream backs off.

## 4. Failure isolation and backoff

Each stream has its own `StreamBackoff` gate: `base * 2^(failures-1)`, capped at
`COLLECTOR_SYNC_BACKOFF_MAX_SECONDS`. While gated, a push returns immediately
without issuing a request, so a hot loop cannot hammer a down hub.

`sync_once()` runs both streams and catches per-stream errors, so:

- A failing relay-event stream cannot stall sensor readings.
- One bad batch cannot stop the sync service.
- Collection and relay/schedule operation continue regardless — neither path
  touches the network.

Errors are logged and stored with `sync_queue.redact()`, which masks the
collector API token before it can reach a log file or the `last_sync_error`
column.

Shutdown is cooperative: the loop only ever sleeps on the stop event, so
`stop()` interrupts a long backoff instead of waiting it out, and `sync_once()`
returns without uploading once cancellation is set.

## 5. Duplicate protection

The hub de-duplicates on `(collector_id, local_record_id)`:

- Unique indexes `uq_sensor_readings_collector_local` and
  `uq_relay_events_collector_local` are the backstop.
- Each batch does one lookup for ids it already holds, so a retried batch of
  200 costs one query rather than 200.
- Ids repeated *within* one batch collapse to a single record.
- `local_record_id` is unique per collector, not globally — two collectors may
  legitimately generate the same id.

Because the id is assigned at insert and never regenerated, **the lost-response
case is safe**: the collector cannot tell "hub never got it" from "hub got it,
reply vanished", so it retries, and the retry comes back as duplicates.

## 6. Hub ingestion endpoints

Both require `X-Collector-Token` and return the same `SyncBatchOut` shape:
`accepted`, `duplicates`, `rejected` (each with a reason), plus counts.

```
POST /api/collector/readings/batch
POST /api/collector/relay-events/batch
```

Validation, in order:

1. Bearer token (`require_collector_token`) → 401.
2. `collector_id` against `MACHINE_KEY_RE` → 422.
3. Batch size against `HUB_MAX_BATCH_SIZE` → 413.
4. Per-record Pydantic validation → 422: `local_record_id` pattern and length,
   non-empty `sensor_name`, temperature within −40…185 °C and humidity within
   0…100 % (mirroring `arduino_protocol`'s hard ranges), field length caps.
5. Per-record semantic checks that reject one record without failing the batch:
   unknown `relay_id`, and timestamps more than 24 h in the future (a broken
   collector clock).

The whole batch commits in **one transaction**. On `IntegrityError` — two
collector threads racing the same batch — it rolls back and returns 409; the
rows are on the hub either way, so the retry returns duplicates. Any other
database error rolls back and returns a generic 500. No response body carries a
stack trace.

Timestamps are normalized to naive UTC on the way in, matching the convention in
`models.utcnow()`.

## 7. Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `COLLECTOR_SYNC_BATCH_SIZE` | 200 | Max records per upload. Keep below `HUB_MAX_BATCH_SIZE`. |
| `HUB_MAX_BATCH_SIZE` | 500 | Hub-side cap; larger batches get 413. |
| `COLLECTOR_SYNC_BACKOFF_BASE_SECONDS` | 2.0 | First retry delay. |
| `COLLECTOR_SYNC_BACKOFF_MAX_SECONDS` | 300.0 | Backoff ceiling. |
| `COLLECTOR_PUSH_INTERVAL_SECONDS` | 10 | Heartbeat + sync cadence. |
| `COLLECTOR_POLL_INTERVAL_SECONDS` | 5 | Schedule/command poll cadence. |

## 8. Upgrading an existing database

`init_db()` stays additive — `create_all` plus `ALTER TABLE ADD COLUMN`, no
Alembic. On an existing database it adds the sync columns, creates the new
tables, and creates indexes that `create_all` skips because their table already
exists.

Rows that predate Stage 3 are backfilled as **already synced**
(`synced_at = recorded_at` / `created_at`, `collector_id = machine_key`). The
old watermark loop had already shipped them; leaving them `NULL` would make an
upgrade re-upload the entire history on first boot.

Known limitation: because the unique indexes are created after the fact, a
database that somehow already holds conflicting `(collector_id,
local_record_id)` pairs will log a warning and skip that index. Duplicate
protection then rests on the explicit per-batch lookup, which is still correct
— just without the database-level backstop. A real migration system (Alembic,
Stage 7) would resolve the conflict instead of skipping.
