"""Event ingestion: simulator chunks -> SQLite counters + audit, exactly once.

How it stays correct across crashes:
- We never trust what /sim/advance returned. Each sync asks "which chunks exist after my cursor?"
  (history and events both, merged by chunk number), so a crash between advance and ingest loses nothing.
- A chunk is read fully into an in-memory ChunkDelta, then committed with the cursor in ONE transaction
  (store.commit_chunk). Killed mid-chunk -> nothing committed -> the chunk is fetched again on restart.
- Events with event_id <= cursor are skipped, a second guard against double counting.

History (before D0) is just chunk 0..k of kind "history"; the first sync ingests it and that is the
baseline later modules compare against.
"""
import argparse
import logging
import time

import httpx

from controller import store
from controller.client import Finished, SimClient

log = logging.getLogger("ingest")

CHUNK_ATTEMPTS = 4  # re-fetch a chunk this many times if the stream breaks mid-way


def build_delta(chunk_no, events, last_event_id):
    """Pure function: turn one chunk's events into a ChunkDelta. Skips events already committed."""
    d = store.ChunkDelta(chunk=chunk_no, last_event_id=last_event_id)
    for ev in events:
        eid = ev["event_id"]
        if eid <= last_event_id:
            continue
        d.last_event_id = eid
        t = ev["type"]
        if t == "call":
            d.add_call(ev)
        elif t == "lead":
            d.add_lead(ev)
        elif t == "campaign_created":
            d.campaigns.append((ev["campaign_id"], ev["workflow_id"], ev["ts"],
                                ev["config_version_at_creation"], ev["leads"]))
        elif t == "config_changed":
            _on_config_changed(d, ev)
        elif t == "approval_decided":
            _on_approval_decided(d, ev)
    return d


def _on_config_changed(d, ev):
    actor = ev["actor"]
    dims = ", ".join(ev.get("changed_dims") or []) or "nothing"
    if actor == "supervisor":
        # Supervisor edits bypass governance. We record them; governance always proposes against the
        # latest version, so we never silently overwrite one.
        kind, who, summary = "supervisor_edit", "supervisor", f"supervisor changed {dims} -> v{ev['version']}"
    else:
        kind, who, summary = "config_applied", actor, f"{actor} changed {dims} -> v{ev['version']}"
        if ev.get("proposal_id"):
            # The event is the ground truth that our change landed, even if we crashed before
            # recording the apply response ourselves.
            d.applied.append((ev["proposal_id"], ev["ts"]))
    d.audits.append(dict(sim_ts=ev["ts"], workflow_id=ev["workflow_id"], kind=kind, summary=summary,
                         actor=who, proposal_id=ev.get("proposal_id"), approval_id=ev.get("approval_id"),
                         evidence={"version": ev["version"], "changed_dims": ev.get("changed_dims")}))


def _on_approval_decided(d, ev):
    reasons = "; ".join(map(str, ev.get("reasons") or [])) or None
    d.approvals.append((ev["approval_id"], ev["status"], ev["ts"], reasons))
    d.audits.append(dict(sim_ts=ev["ts"], workflow_id=ev["workflow_id"], kind="approval_decided",
                         summary=f"approver: {ev['status']}" + (f" ({reasons})" if reasons else ""),
                         actor="approver", approval_id=ev["approval_id"],
                         evidence={"reasons": ev.get("reasons") or []}))


class Ingestor:
    def __init__(self, conn, client):
        self.conn, self.client = conn, client

    def pending_chunks(self):
        last_chunk, _ = store.get_cursor(self.conn)
        chunks = (self.client.list_chunks("history", after=last_chunk)
                  + self.client.list_chunks("events", after=last_chunk))
        return sorted(chunks, key=lambda c: c["chunk"])

    def sync(self):
        """Ingest every chunk after the cursor. Returns (chunks, events) committed."""
        n_chunks = n_events = 0
        for ch in self.pending_chunks():
            n_events += self._ingest_chunk(ch["chunk"])
            n_chunks += 1
        return n_chunks, n_events

    def _ingest_chunk(self, chunk_no):
        for attempt in range(CHUNK_ATTEMPTS):
            _, last_event_id = store.get_cursor(self.conn)
            try:
                delta = build_delta(chunk_no, self.client.iter_chunk(chunk_no), last_event_id)
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                # Stream broke mid-chunk: the half-built delta is dropped, nothing was committed.
                log.warning("chunk %d attempt %d failed: %s", chunk_no, attempt + 1, e)
                if attempt == CHUNK_ATTEMPTS - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            store.commit_chunk(self.conn, delta)
            return delta.last_event_id - last_event_id


def main():
    """Ingest-only mode (no decisions): used for crash tests and load timing."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--until-minute", type=int, default=None, help="stop advancing at this sim minute")
    ap.add_argument("--no-advance", action="store_true", help="only ingest chunks that already exist")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn, client = store.open_db(a.db), SimClient(a.url)
    ing = Ingestor(conn, client)
    t0 = time.time()
    total = ing.sync()[1]
    while not a.no_advance:
        st = client.state()
        if st["finished"] or (a.until_minute is not None and st["sim_minute"] >= a.until_minute):
            break
        try:
            client.advance(60)
        except Finished:
            break
        total += ing.sync()[1]
    st = client.state()
    log.info("done: sim %s, %d events in %.1fs (%.0f ev/s), cursor %s",
             st["ts"], total, time.time() - t0, total / max(time.time() - t0, 1e-9), store.get_cursor(conn))


if __name__ == "__main__":
    main()
