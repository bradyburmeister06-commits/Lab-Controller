"""Stage 3: local-first storage, the collector sync queue, and duplicate protection.

The behaviours under test are the ones that decide whether a lab machine loses
data during a hub outage: every record is written to SQLite before any upload is
attempted, a failed upload leaves the record pending, and a retried batch is
absorbed as duplicates rather than stored twice.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.models import RelayEvent, SensorReading, SyncState, new_record_id
from app.db.session import SessionLocal
from app.main import app
from app.services import sync_queue
from app.services.collector_agent import CollectorAgent, SyncError
from app.services.relay_controller import build_relay_controller
from app.services.relay_service import apply_state
from app.services.sensor_service import save_reading
from app.services.sync_queue import STREAM_READINGS, STREAM_RELAY_EVENTS


TOKEN = "change-me-collector-token"
TOKEN_HEADER = {"X-Collector-Token": TOKEN}

READINGS_BATCH = "/api/collector/readings/batch"
EVENTS_BATCH = "/api/collector/relay-events/batch"


def _cid(prefix: str) -> str:
    """Unique collector id per test — the suite shares one SQLite file."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _reading(**overrides) -> dict:
    body = {
        "local_record_id": new_record_id(),
        "sensor_name": "chamber-a",
        "temperature": 22.5,
        "relative_humidity": 45.0,
    }
    body.update(overrides)
    return body


def _event(**overrides) -> dict:
    body = {
        "local_record_id": new_record_id(),
        "relay_id": "relay-1",
        "state": True,
        "action": "set",
        "trigger_source": "collector",
        "success": True,
    }
    body.update(overrides)
    return body


class _StubTransport(httpx.BaseTransport):
    """Drives the agent's HTTP paths without a live hub.

    The handler decides each response, so a test can make requests fail, time
    out, or succeed, and can inspect ``requests`` to assert none were sent.
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _agent_with(monkeypatch, handler, collector_id: str) -> tuple[CollectorAgent, _StubTransport]:
    settings = get_settings().model_copy()
    settings.collector_id = collector_id
    settings.collector_sync_batch_size = 5
    settings.collector_sync_backoff_base_seconds = 0.1
    settings.collector_sync_backoff_max_seconds = 0.1
    agent = CollectorAgent(settings, build_relay_controller(settings))
    transport = _StubTransport(handler)
    monkeypatch.setattr(
        agent,
        "_client",
        lambda: httpx.Client(base_url="http://hub.test", transport=transport),
    )
    return agent, transport


# --- 1. local storage happens before any upload ---


def test_collector_saves_reading_before_upload():
    """A reading is durable the moment it is taken, with no hub involved."""
    cid = _cid("save-reading")
    with SessionLocal() as db:
        reading = save_reading(
            db,
            sensor_name="chamber-a",
            temperature=21.0,
            relative_humidity=44.0,
            raw_payload="temp=21.0,rh=44.0",
            machine_key=cid,
        )
        reading_id = reading.id

    with SessionLocal() as db:
        stored = db.get(SensorReading, reading_id)
        assert stored is not None
        assert stored.collector_id == cid
        assert stored.local_record_id, "a local record id is assigned at insert time"
        assert stored.synced_at is None, "unsynced until the hub confirms it"
        assert stored.sync_attempts == 0
        pending = sync_queue.pending_records(db, STREAM_READINGS, cid, limit=10)
        assert [r.id for r in pending] == [reading_id]


def test_collector_saves_relay_event_before_upload():
    cid = _cid("save-event")
    settings = get_settings()
    controller = build_relay_controller(settings)
    with SessionLocal() as db:
        _, event = apply_state(
            db, "relay-1", True, controller, trigger_source="scheduler", machine_key=cid
        )
        event_id = event.id

    with SessionLocal() as db:
        stored = db.get(RelayEvent, event_id)
        assert stored is not None
        assert stored.collector_id == cid
        assert stored.local_record_id
        assert stored.synced_at is None
        pending = sync_queue.pending_records(db, STREAM_RELAY_EVENTS, cid, limit=10)
        assert event_id in [r.id for r in pending]


# --- 2. retry and backlog behaviour ---


def test_failed_upload_is_retried(monkeypatch):
    """A failed batch stays pending, records the error, and succeeds on retry."""
    cid = _cid("retry")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)

    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("hub unreachable", request=request)
        return httpx.Response(200, json={"accepted": [], "duplicates": [], "rejected": []})

    agent, _ = _agent_with(monkeypatch, handler, cid)

    with pytest.raises(SyncError):
        agent.push_pending_readings()

    with SessionLocal() as db:
        rows = sync_queue.pending_records(db, STREAM_READINGS, cid, limit=10)
        assert len(rows) == 1, "a failed upload must not consume the record"
        assert rows[0].sync_attempts == 1
        assert rows[0].last_sync_error
        assert TOKEN not in rows[0].last_sync_error

    # The hub comes back and confirms the record this time.
    def confirming(request: httpx.Request) -> httpx.Response:
        ids = [r["local_record_id"] for r in json.loads(request.read())["readings"]]
        return httpx.Response(200, json={"accepted": ids, "duplicates": [], "rejected": []})

    agent2, _ = _agent_with(monkeypatch, confirming, cid)
    assert agent2.push_pending_readings() == 1

    with SessionLocal() as db:
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 0


def test_backlog_uploads_after_reconnection(monkeypatch):
    """Readings collected during an outage all ship once the link returns."""
    cid = _cid("backlog")
    with SessionLocal() as db:
        for i in range(12):
            save_reading(db, "chamber-a", 20.0 + i, 40.0, None, machine_key=cid)

    offline = {"down": True}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if offline["down"]:
            raise httpx.ConnectError("hub unreachable", request=request)
        ids = [r["local_record_id"] for r in json.loads(request.read())["readings"]]
        seen.extend(ids)
        return httpx.Response(200, json={"accepted": ids, "duplicates": [], "rejected": []})

    agent, _ = _agent_with(monkeypatch, handler, cid)

    with pytest.raises(SyncError):
        agent.push_pending_readings()
    with SessionLocal() as db:
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 12

    offline["down"] = False
    # Batch size is 5, so the 12-record backlog drains over three passes.
    agent._backoff[STREAM_READINGS].on_success()
    total = 0
    for _ in range(5):
        total += agent.push_pending_readings()
    assert total == 12
    assert len(seen) == 12

    with SessionLocal() as db:
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 0


def test_retry_after_response_timeout_does_not_duplicate(monkeypatch):
    """The hub stored the batch but the response was lost.

    The collector cannot tell this apart from a real failure, so it retries. The
    stable local_record_id is what turns the second delivery into duplicates
    instead of a second copy of every reading.
    """
    cid = _cid("timeout")
    with SessionLocal() as db:
        for i in range(3):
            save_reading(db, "chamber-a", 20.0 + i, 40.0, None, machine_key=cid)

    calls = {"n": 0}
    # Tests share one SQLite file, so the hub's copies are filed under a
    # separate id. In a real split deployment this separation is the network.
    hub_cid = f"{cid}-hub"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = json.loads(request.read())
        payload["collector_id"] = hub_cid
        # Both deliveries really reach the hub; only the first response is lost.
        with TestClient(app) as client:
            response = client.post(READINGS_BATCH, headers=TOKEN_HEADER, json=payload)
        assert response.status_code == 200, response.text
        if calls["n"] == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json=response.json())

    agent, _ = _agent_with(monkeypatch, handler, cid)

    with pytest.raises(SyncError):
        agent.push_pending_readings()

    agent._backoff[STREAM_READINGS].on_success()
    assert agent.push_pending_readings() == 3

    with SessionLocal() as db:
        hub_rows = db.query(SensorReading).filter(SensorReading.collector_id == hub_cid).all()
        assert len(hub_rows) == 3, "the re-delivered batch must not be stored twice"
        assert len({r.local_record_id for r in hub_rows}) == 3
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 0


def test_one_failing_stream_does_not_stop_the_other(monkeypatch):
    """Relay-event failures must not stall sensor readings, or vice versa."""
    cid = _cid("isolation")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)
        apply_state(
            db, "relay-1", True, build_relay_controller(get_settings()),
            trigger_source="scheduler", machine_key=cid,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if "relay-events" in request.url.path:
            raise httpx.ConnectError("relay ingest down", request=request)
        ids = [r["local_record_id"] for r in json.loads(request.read())["readings"]]
        return httpx.Response(200, json={"accepted": ids, "duplicates": [], "rejected": []})

    agent, _ = _agent_with(monkeypatch, handler, cid)
    # sync_once swallows per-stream failures so the service keeps running.
    agent.sync_once()

    with SessionLocal() as db:
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 0
        assert sync_queue.pending_count(db, STREAM_RELAY_EVENTS, cid) == 1


def test_sync_state_tracks_last_success_and_pending(monkeypatch):
    cid = _cid("state")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)

    def handler(request: httpx.Request) -> httpx.Response:
        ids = [r["local_record_id"] for r in json.loads(request.read())["readings"]]
        return httpx.Response(200, json={"accepted": ids, "duplicates": [], "rejected": []})

    agent, _ = _agent_with(monkeypatch, handler, cid)
    agent.push_pending_readings()

    assert agent.last_sync_at is not None
    assert agent.pending_readings == 0
    with SessionLocal() as db:
        state = db.get(SyncState, (cid, STREAM_READINGS))
        assert state is not None
        assert state.last_success_at is not None
        assert state.consecutive_failures == 0
        assert state.synced_total == 1


def test_backoff_is_exponential_and_capped():
    assert sync_queue.backoff_seconds(1, base=2.0, maximum=300.0) == 2.0
    assert sync_queue.backoff_seconds(2, base=2.0, maximum=300.0) == 4.0
    assert sync_queue.backoff_seconds(3, base=2.0, maximum=300.0) == 8.0
    assert sync_queue.backoff_seconds(50, base=2.0, maximum=300.0) == 300.0


def test_backoff_gate_defers_next_attempt(monkeypatch):
    """After a failure the stream is gated, so a hot loop cannot hammer the hub."""
    cid = _cid("gate")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    agent, transport = _agent_with(monkeypatch, handler, cid)
    agent._backoff[STREAM_READINGS].base = 60.0
    agent._backoff[STREAM_READINGS].maximum = 60.0

    with pytest.raises(SyncError):
        agent.push_pending_readings()
    attempts = len(transport.requests)

    # Gated: this call returns without issuing a request.
    assert agent.push_pending_readings() == 0
    assert len(transport.requests) == attempts


def test_stop_is_honoured_during_shutdown(monkeypatch):
    cid = _cid("shutdown")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no upload should be attempted after stop()")

    agent, transport = _agent_with(monkeypatch, handler, cid)
    agent.stop()
    assert agent.cancelled is True
    agent.sync_once()
    assert transport.requests == []


def test_upload_errors_do_not_leak_the_token(monkeypatch):
    cid = _cid("redact")
    with SessionLocal() as db:
        save_reading(db, "chamber-a", 20.0, 40.0, None, machine_key=cid)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"upstream rejected token {TOKEN}")

    agent, _ = _agent_with(monkeypatch, handler, cid)
    with pytest.raises(SyncError):
        agent.push_pending_readings()

    with SessionLocal() as db:
        row = sync_queue.pending_records(db, STREAM_READINGS, cid, limit=1)[0]
        assert TOKEN not in (row.last_sync_error or "")
        assert "redacted" in row.last_sync_error


# --- 3. hub-side duplicate protection ---


def test_duplicate_reading_is_ignored():
    cid = _cid("dup-reading")
    reading = _reading()
    with TestClient(app) as client:
        first = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                            json={"collector_id": cid, "readings": [reading]})
        assert first.status_code == 200, first.text
        assert first.json()["accepted"] == [reading["local_record_id"]]

        second = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                             json={"collector_id": cid, "readings": [reading]})
        assert second.status_code == 200, "a duplicate is a success, not a failure"
        body = second.json()
        assert body["accepted"] == []
        assert body["duplicates"] == [reading["local_record_id"]]

    with SessionLocal() as db:
        rows = db.query(SensorReading).filter(
            SensorReading.collector_id == cid,
            SensorReading.local_record_id == reading["local_record_id"],
        ).all()
        assert len(rows) == 1


def test_duplicate_relay_event_is_ignored():
    cid = _cid("dup-event")
    event = _event()
    with TestClient(app) as client:
        first = client.post(EVENTS_BATCH, headers=TOKEN_HEADER,
                            json={"collector_id": cid, "events": [event]})
        assert first.status_code == 200, first.text
        assert first.json()["accepted_count"] == 1

        second = client.post(EVENTS_BATCH, headers=TOKEN_HEADER,
                             json={"collector_id": cid, "events": [event]})
        assert second.status_code == 200
        assert second.json()["duplicates"] == [event["local_record_id"]]

    with SessionLocal() as db:
        rows = db.query(RelayEvent).filter(
            RelayEvent.collector_id == cid,
            RelayEvent.local_record_id == event["local_record_id"],
        ).all()
        assert len(rows) == 1


def test_sending_same_batch_twice():
    cid = _cid("same-batch")
    readings = [_reading(temperature=20.0 + i) for i in range(4)]
    payload = {"collector_id": cid, "readings": readings}
    with TestClient(app) as client:
        first = client.post(READINGS_BATCH, headers=TOKEN_HEADER, json=payload).json()
        second = client.post(READINGS_BATCH, headers=TOKEN_HEADER, json=payload).json()

    assert first["accepted_count"] == 4
    assert second["accepted_count"] == 0
    assert second["duplicate_count"] == 4

    with SessionLocal() as db:
        assert db.query(SensorReading).filter(SensorReading.collector_id == cid).count() == 4


def test_partial_duplicates_inside_batch():
    """A batch mixing already-stored and new records stores only the new ones."""
    cid = _cid("partial")
    old = [_reading(temperature=20.0), _reading(temperature=21.0)]
    new = [_reading(temperature=22.0), _reading(temperature=23.0)]

    with TestClient(app) as client:
        client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                    json={"collector_id": cid, "readings": old}).raise_for_status()
        overlapping = client.post(
            READINGS_BATCH, headers=TOKEN_HEADER,
            json={"collector_id": cid, "readings": old + new},
        ).json()

    assert sorted(overlapping["duplicates"]) == sorted(r["local_record_id"] for r in old)
    assert sorted(overlapping["accepted"]) == sorted(r["local_record_id"] for r in new)

    with SessionLocal() as db:
        assert db.query(SensorReading).filter(SensorReading.collector_id == cid).count() == 4


def test_duplicate_ids_repeated_within_one_batch():
    """The same id twice in a single request is still one record."""
    cid = _cid("self-dup")
    reading = _reading()
    with TestClient(app) as client:
        body = client.post(
            READINGS_BATCH, headers=TOKEN_HEADER,
            json={"collector_id": cid, "readings": [reading, reading]},
        ).json()

    assert body["accepted_count"] == 1
    assert body["duplicate_count"] == 1
    with SessionLocal() as db:
        assert db.query(SensorReading).filter(SensorReading.collector_id == cid).count() == 1


def test_same_local_record_id_from_different_collectors_both_stored():
    """local_record_id is only unique per collector, not globally."""
    shared = new_record_id()
    a, b = _cid("tenant-a"), _cid("tenant-b")
    with TestClient(app) as client:
        for cid in (a, b):
            r = client.post(
                READINGS_BATCH, headers=TOKEN_HEADER,
                json={"collector_id": cid, "readings": [_reading(local_record_id=shared)]},
            )
            assert r.json()["accepted_count"] == 1

    with SessionLocal() as db:
        assert db.query(SensorReading).filter(
            SensorReading.local_record_id == shared
        ).count() == 2


# --- 4. batch endpoint validation ---


def test_batch_endpoints_require_token():
    with TestClient(app) as client:
        assert client.post(READINGS_BATCH, json={"collector_id": "c1", "readings": []}).status_code == 401
        assert client.post(EVENTS_BATCH, json={"collector_id": "c1", "events": []}).status_code == 401


def test_batch_endpoints_reject_bad_token():
    bad = {"X-Collector-Token": "wrong-token"}
    with TestClient(app) as client:
        assert client.post(READINGS_BATCH, headers=bad,
                           json={"collector_id": "c1", "readings": []}).status_code == 401
        assert client.post(EVENTS_BATCH, headers=bad,
                           json={"collector_id": "c1", "events": []}).status_code == 401


def test_batch_rejects_invalid_collector_id():
    with TestClient(app) as client:
        r = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                        json={"collector_id": "Not A Valid Key!", "readings": []})
        assert r.status_code == 422
        assert "detail" in r.json()


def test_oversized_batch_is_rejected():
    cid = _cid("oversize")
    limit = get_settings().hub_max_batch_size
    readings = [_reading() for _ in range(limit + 1)]
    with TestClient(app) as client:
        r = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                        json={"collector_id": cid, "readings": readings})
    assert r.status_code == 413
    assert str(limit) in r.json()["detail"]

    with SessionLocal() as db:
        assert db.query(SensorReading).filter(SensorReading.collector_id == cid).count() == 0


def test_oversized_relay_event_batch_is_rejected():
    cid = _cid("oversize-events")
    limit = get_settings().hub_max_batch_size
    events = [_event() for _ in range(limit + 1)]
    with TestClient(app) as client:
        r = client.post(EVENTS_BATCH, headers=TOKEN_HEADER,
                        json={"collector_id": cid, "events": events})
    assert r.status_code == 413


@pytest.mark.parametrize(
    "bad",
    [
        {"temperature": 999.0},
        {"temperature": -300.0},
        {"relative_humidity": 150.0},
        {"relative_humidity": -1.0},
        {"local_record_id": ""},
        {"sensor_name": ""},
    ],
)
def test_invalid_reading_is_rejected(bad):
    """Out-of-range and malformed readings are refused with a clean 422."""
    cid = _cid("invalid")
    with TestClient(app) as client:
        r = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                        json={"collector_id": cid, "readings": [_reading(**bad)]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Traceback" not in str(detail), "validation errors must not leak stack traces"

    with SessionLocal() as db:
        assert db.query(SensorReading).filter(SensorReading.collector_id == cid).count() == 0


def test_malformed_batch_body_is_rejected():
    with TestClient(app) as client:
        r = client.post(READINGS_BATCH, headers=TOKEN_HEADER, json={"readings": "not-a-list"})
    assert r.status_code == 422
    assert "Traceback" not in r.text


def test_future_timestamp_is_rejected_per_record():
    """A broken collector clock rejects one record without failing the batch."""
    cid = _cid("clock")
    good = _reading()
    bad = _reading(recorded_at="2999-01-01T00:00:00")
    with TestClient(app) as client:
        body = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                           json={"collector_id": cid, "readings": [good, bad]}).json()

    assert body["accepted"] == [good["local_record_id"]]
    assert body["rejected_count"] == 1
    assert body["rejected"][0]["local_record_id"] == bad["local_record_id"]


def test_unknown_relay_id_is_rejected_not_stored():
    cid = _cid("bad-relay")
    good = _event(relay_id="relay-1")
    bad = _event(relay_id="relay-does-not-exist")
    with TestClient(app) as client:
        body = client.post(EVENTS_BATCH, headers=TOKEN_HEADER,
                           json={"collector_id": cid, "events": [good, bad]}).json()

    assert body["accepted"] == [good["local_record_id"]]
    assert body["rejected_count"] == 1
    assert "unknown relay_id" in body["rejected"][0]["reason"]


def test_relay_event_batch_updates_relay_state():
    cid = _cid("relay-state")
    with TestClient(app) as client:
        client.post(
            EVENTS_BATCH, headers=TOKEN_HEADER,
            json={"collector_id": cid, "events": [_event()], "relay_states": {"relay-2": True}},
        ).raise_for_status()
        relays = client.get("/api/public/relays").json()
        assert next(r for r in relays if r["id"] == "relay-2")["is_on"] is True

        client.post(
            EVENTS_BATCH, headers=TOKEN_HEADER,
            json={"collector_id": cid, "events": [], "relay_states": {"relay-2": False}},
        ).raise_for_status()


def test_empty_batch_is_accepted():
    """An empty batch is a valid no-op, so an idle collector isn't error-logged."""
    cid = _cid("empty")
    with TestClient(app) as client:
        r = client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                        json={"collector_id": cid, "readings": []})
    assert r.status_code == 200
    assert r.json()["accepted_count"] == 0


def test_ingested_records_are_marked_synced_on_the_hub():
    """Hub-side copies are already at rest — they must never enter a sync queue."""
    cid = _cid("hub-synced")
    with TestClient(app) as client:
        client.post(READINGS_BATCH, headers=TOKEN_HEADER,
                    json={"collector_id": cid, "readings": [_reading()]}).raise_for_status()

    with SessionLocal() as db:
        row = db.query(SensorReading).filter(SensorReading.collector_id == cid).one()
        assert row.synced_at is not None
        assert sync_queue.pending_count(db, STREAM_READINGS, cid) == 0


def test_existing_collector_endpoints_still_work():
    """Stage 2's register/heartbeat/poll contract is unchanged."""
    cid = _cid("compat")
    with TestClient(app) as client:
        assert client.post("/api/collector/register", headers=TOKEN_HEADER,
                           json={"collector_id": cid, "name": "Compat"}).status_code == 200
        assert client.post("/api/collector/heartbeat", headers=TOKEN_HEADER,
                           json={"collector_id": cid}).status_code == 200
        poll = client.get("/api/collector/poll", headers=TOKEN_HEADER,
                          params={"collector_id": cid})
        assert poll.status_code == 200
        assert {r["id"] for r in poll.json()["relays"]} == {"relay-1", "relay-2", "relay-3"}
