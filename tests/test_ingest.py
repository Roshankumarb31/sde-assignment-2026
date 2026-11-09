"""Ingest: exactly-once counting across replays and broken streams, and event routing to audit/proposals."""
import httpx

from controller import store
from controller.ingest import Ingestor

TS = "2027-01-04T10:15:00+05:30"


def call(eid, did="D1", connected=True):
    return {"type": "call", "event_id": eid, "ts": TS, "workflow_id": "wf", "config_version": 1,
            "cohort_id": "default", "connected": connected, "engaged": False,
            "qa": {"hallucination_flag": False} if connected else None,
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": did, "prompt_version": "P1"}}


class FakeClient:
    """Serves fixed chunks. break_once=(chunk, after_n) drops the stream once mid-chunk."""
    def __init__(self, chunks, break_once=None):
        self.chunks, self.break_once = chunks, break_once

    def list_chunks(self, kind, after=-1):
        return [{"chunk": n} for n, (k, _) in sorted(self.chunks.items()) if k == kind and n > after]

    def iter_chunk(self, n):
        for i, ev in enumerate(self.chunks[n][1]):
            if self.break_once and self.break_once[0] == n and i == self.break_once[1]:
                self.break_once = None
                raise httpx.ReadError("connection reset")
            yield ev


def dials(conn):
    return store.total_stats(conn, "wf", "2027-01-01T00")["dials"]


def test_history_then_events_counted_once(tmp_path):
    conn = store.open_db(tmp_path / "s.db")
    fc = FakeClient({0: ("history", [call(1), call(2)]), 1: ("events", [call(3), call(4, connected=False)])})
    ing = Ingestor(conn, fc)
    assert ing.sync() == (2, 4)
    assert ing.sync() == (0, 0)          # nothing new
    assert dials(conn) == 4
    assert store.get_cursor(conn) == (1, 4)


def test_broken_stream_is_refetched_not_half_counted(tmp_path, monkeypatch):
    monkeypatch.setattr("controller.ingest.time.sleep", lambda s: None)
    conn = store.open_db(tmp_path / "s.db")
    fc = FakeClient({0: ("events", [call(i) for i in range(1, 11)])}, break_once=(0, 6))
    Ingestor(conn, fc).sync()
    assert dials(conn) == 10             # not 16: the first 6 were never committed


def test_restart_with_new_process_resumes(tmp_path):
    path = tmp_path / "s.db"
    fc = FakeClient({0: ("history", [call(1)]), 1: ("events", [call(2)])})
    Ingestor(store.open_db(path), fc).sync()
    fc.chunks[2] = ("events", [call(3)])  # world moved on while we were down
    conn = store.open_db(path)            # "restarted" controller
    assert Ingestor(conn, fc).sync() == (1, 1)
    assert dials(conn) == 3


def test_supervisor_edit_and_approval_events(tmp_path):
    conn = store.open_db(tmp_path / "s.db")
    store.insert_proposal(conn, "p1", "wf", 3, {}, TS)
    store.update_proposal(conn, "p1", approval_id="apr_1")
    events = [
        {"type": "config_changed", "event_id": 1, "ts": TS, "workflow_id": "wf", "version": 4,
         "actor": "supervisor", "changed_dims": ["prompt_version"], "proposal_id": None, "approval_id": None},
        {"type": "approval_decided", "event_id": 2, "ts": TS, "workflow_id": "wf", "approval_id": "apr_1",
         "status": "approved", "reasons": []},
        {"type": "config_changed", "event_id": 3, "ts": TS, "workflow_id": "wf", "version": 5,
         "actor": "controller_approved", "changed_dims": ["voice"], "proposal_id": "p1", "approval_id": "apr_1"},
    ]
    Ingestor(conn, FakeClient({0: ("events", events)})).sync()
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM audit ORDER BY id")]
    assert kinds == ["supervisor_edit", "approval_decided", "config_applied"]
    p = store.get_proposal(conn, "p1")
    assert p["status"] == "applied" and p["applied_at"] == TS
