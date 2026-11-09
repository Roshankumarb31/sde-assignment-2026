"""Store invariants that crash safety depends on."""
import pytest

from controller import store


def call(eid, ts="2027-01-04T10:15:00+05:30", connected=True, engaged=False, halluc=False, did="D1"):
    return {"type": "call", "event_id": eid, "ts": ts, "workflow_id": "wf", "config_version": 1,
            "cohort_id": "default", "connected": connected, "engaged": engaged,
            "qa": {"hallucination_flag": halluc} if connected else None,
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": did, "prompt_version": "P1"}}


def delta_for(chunk, events):
    d = store.ChunkDelta(chunk=chunk, last_event_id=max(e["event_id"] for e in events))
    for e in events:
        d.add_call(e)
    return d


def test_chunk_is_counted_once(tmp_path):
    conn = store.open_db(tmp_path / "s.db")
    d = delta_for(0, [call(1), call(2, connected=False), call(3, engaged=True, halluc=True)])
    assert store.commit_chunk(conn, d) is True
    assert store.commit_chunk(conn, d) is False  # replay after restart is a no-op
    assert store.get_cursor(conn) == (0, 3)
    assert store.total_stats(conn, "wf", "2027-01-04T00") == {"dials": 3, "connected": 2, "engaged": 1, "halluc": 1}


def test_failed_chunk_leaves_no_trace(tmp_path):
    conn = store.open_db(tmp_path / "s.db")
    d = delta_for(0, [call(1)])
    d.audits.append({"sim_ts": "x", "kind": "k", "summary": "s", "actor": None})  # NOT NULL violation
    with pytest.raises(Exception):
        store.commit_chunk(conn, d)
    assert store.get_cursor(conn) == (-1, 0)
    assert store.total_stats(conn, "wf", "2027-01-04T00")["dials"] == 0


def test_state_survives_reopen(tmp_path):
    path = tmp_path / "s.db"
    conn = store.open_db(path)
    store.commit_chunk(conn, delta_for(4, [call(10, did="D2")]))
    store.insert_proposal(conn, "p1", "wf", 1, {"a": 1}, "t0")
    store.insert_proposal(conn, "p1", "wf", 1, {"a": 1}, "t0")  # idempotent retry
    store.kv_put(conn, "flagged_dids:wf", ["D2"])
    conn.close()

    conn = store.open_db(path)
    assert store.get_cursor(conn) == (4, 10)
    assert len(store.open_proposals(conn, "wf")) == 1
    assert store.kv_get(conn, "flagged_dids:wf") == ["D2"]
    assert store.sum_stats(conn, "wf", "caller_id", "2027-01-04T00")["D2"]["dials"] == 1


def test_late_event_does_not_undo_applied(tmp_path):
    conn = store.open_db(tmp_path / "s.db")
    store.insert_proposal(conn, "p1", "wf", 1, {}, "t0")
    store.update_proposal(conn, "p1", approval_id="apr_1", status="applied")
    store.set_status_by_approval(conn, "apr_1", "approved", "t1")
    assert store.get_proposal(conn, "p1")["status"] == "applied"
