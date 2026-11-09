"""The controller loop: advance -> ingest -> detect -> govern, until the scenario finishes.

    python -m controller.main --url http://127.0.0.1:8700 --db state.db

Restart-safe by construction: every step reads its position from the store (cursor, proposals, intents),
so a restarted process just continues. Nothing is kept only in memory between ticks.
"""
import argparse
import logging
import os
import time
from datetime import datetime, timedelta

from controller import store
from controller.client import Finished, SimClient
from controller.governance import Governance
from controller.incident import IncidentDetector
from controller.ingest import Ingestor

log = logging.getLogger("controller")

FAST_STEP = 15   # minutes per advance while an approval request is open (apply soon after a decision)


def open_store_for(client, path):
    """Open the store, or start a fresh one if it belongs to another simulator world.

    The simulator keeps its world in memory: if IT restarts, the world is new and our old state is wrong
    for it. We detect that (different scenario, or our cursor is ahead of the world) and move the old file
    aside instead of silently mixing two worlds."""
    st = client.state()
    conn = store.open_db(path)
    owner = store.kv_get(conn, "world")
    _, last_event_id = store.get_cursor(conn)
    if owner is not None and (owner["scenario"] != st["scenario"] or last_event_id > st["last_event_id"]):
        conn.close()
        aside = f"{path}.other-world-{int(time.time())}"
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.rename(path + suffix, aside + suffix)
        log.warning("state file belonged to another simulator run; moved to %s and starting fresh", aside)
        conn = store.open_db(path)
    # D0 (start of the live scenario) separates history (baselines) from live data.
    d0 = (datetime.fromisoformat(st["ts"]) - timedelta(minutes=st["sim_minute"])).isoformat()
    with store.atomic(conn):
        store.kv_put(conn, "world", {"scenario": st["scenario"], "d0": d0})
    return conn


class Controller:
    def __init__(self, conn, client, step=60):
        self.conn, self.client, self.step = conn, client, step
        self.ingest = Ingestor(conn, client)
        self.gov = Governance(conn, client)
        # Detectors only read the store and file intents; governance is the only thing that changes config.
        self.detectors = [IncidentDetector(conn, client, self.gov)]
        self._hour_mark = None

    def run(self):
        t0 = time.time()
        n, ev = self.ingest.sync()   # history on first start; catch-up after a restart
        st = self.client.state()
        log.info("start %s at %s: ingested %d chunks / %d events (cursor %s), credits %.0f",
                 st["scenario"], st["ts"], n, ev, store.get_cursor(self.conn), st["credits_remaining"])
        self.tick(st["ts"])          # reconcile anything left in flight before the crash
        while not st["finished"]:
            try:
                st = self.client.advance(self._next_step())
            except Finished:
                break
            self.ingest.sync()
            self.tick(st["ts"])
            self._log_hour(st)
        st = self.client.state()
        log.info("finished %s at %s in %.0fs wall, credits left %.0f", st["scenario"], st["ts"],
                 time.time() - t0, st["credits_remaining"])

    def tick(self, now):
        for d in self.detectors:
            d.run(now)
        self.gov.tick(now)

    def _next_step(self):
        waiting = self.conn.execute(
            "SELECT 1 FROM proposals WHERE route = 'approval' AND status IN ('pending', 'approved') LIMIT 1").fetchone()
        return FAST_STEP if waiting else self.step

    def _log_hour(self, st):
        """One line per simulated hour: what an on-call engineer needs without opening the database."""
        hour = st["ts"][:13]
        if hour == self._hour_mark or st["ts"][14:16] != "00":
            return
        since = f"{self._hour_mark}:00" if self._hour_mark else "0000"
        self._hour_mark = hour
        st = self.client.state()     # advance's reply has no event/credit counters
        _, cursor_eid = store.get_cursor(self.conn)
        decisions = self.conn.execute(
            "SELECT COUNT(*) n FROM audit WHERE actor = 'controller' AND sim_ts >= ?", (since,)).fetchone()["n"]
        open_props = len(store.open_proposals(self.conn))
        log.info("%s lag=%d events decisions=%d open_proposals=%d credits=%.0f",
                 st["ts"][:16], st["last_event_id"] - cursor_eid, decisions, open_props, st["credits_remaining"])


def main():
    ap = argparse.ArgumentParser(description="Outcome-first controller")
    ap.add_argument("--url", required=True, help="simulator base URL")
    ap.add_argument("--db", default="state.db", help="SQLite state file (created if missing)")
    ap.add_argument("--step", type=int, default=60, help="minutes per advance (multiple of 15)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per request is noise
    client = SimClient(a.url)
    conn = open_store_for(client, a.db)
    Controller(conn, client, a.step).run()


if __name__ == "__main__":
    main()
