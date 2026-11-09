"""SQLite store: the controller's only memory.

Everything the controller knows lives here, so a kill -9 at any moment loses nothing that was committed.

Design rules:
- WAL mode: readers (explain CLI) never block the writer (main loop).
- One chunk of events = one transaction. Stats, audit rows, proposal updates AND the cursor are written
  together by `commit_chunk`, so a crash either keeps the whole chunk or none of it. Replaying a chunk
  after restart is a no-op because events with event_id <= cursor are skipped.
- Stats are pre-aggregated counters per (workflow, hour, config_version, cohort, dim, value), not raw
  events. ~850k events/day collapse to a few thousand rows/day.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field

DIMS = ("welcome_message", "voice", "caller_id", "prompt_version")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    id            INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    last_chunk    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL
);

-- One row per (workflow, hour bucket, config version, cohort, dimension, option value).
-- Every call adds to 4 rows (one per dim), so summing a single dim gives workflow totals.
-- hour = 'YYYY-MM-DDTHH' in sim local time (sorts lexicographically = chronologically).
CREATE TABLE IF NOT EXISTS stats (
    workflow_id    TEXT    NOT NULL,
    hour           TEXT    NOT NULL,
    config_version INTEGER NOT NULL,
    cohort_id      TEXT    NOT NULL,
    dim            TEXT    NOT NULL,
    value          TEXT    NOT NULL,
    dials          INTEGER NOT NULL DEFAULT 0,
    connected      INTEGER NOT NULL DEFAULT 0,
    engaged        INTEGER NOT NULL DEFAULT 0,
    halluc         INTEGER NOT NULL DEFAULT 0,   -- connected calls flagged by QA
    PRIMARY KEY (workflow_id, hour, config_version, cohort_id, dim, value)
) WITHOUT ROWID;

-- Lead counts by feature combination per day: used to estimate affected_leads for a cohort rule.
CREATE TABLE IF NOT EXISTS lead_counts (
    workflow_id TEXT NOT NULL,
    day         TEXT NOT NULL,   -- 'YYYY-MM-DD'
    lang        TEXT NOT NULL,
    city_tier   TEXT NOT NULL,
    age_band    TEXT NOT NULL,
    value_band  TEXT NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, day, lang, city_tier, age_band, value_band)
) WITHOUT ROWID;

-- Needed for mid_flight=next_campaign_only: which config a campaign is locked to.
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id    TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    created_ts     TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    leads          INTEGER NOT NULL
);

-- Proposal lifecycle (see governance.py):
-- pending -> approved | rejected | expired ; approved -> applied | lapsed ; plus auto_applied, stale.
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  TEXT PRIMARY KEY,   -- hash(workflow, base_version, config): retries are idempotent
    workflow_id  TEXT NOT NULL,
    status       TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,      -- exact body sent to the simulator
    approval_id  TEXT,
    created_at   TEXT NOT NULL,      -- sim time
    decided_at   TEXT,
    applied_at   TEXT,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS proposals_wf_status ON proposals (workflow_id, status);
CREATE INDEX IF NOT EXISTS proposals_approval ON proposals (approval_id);

-- Append-only decision log. Answers "why did X change, who approved it, what happened next".
CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_ts        TEXT NOT NULL,
    workflow_id   TEXT,
    kind          TEXT NOT NULL,     -- e.g. detect, propose, decided, applied, supervisor_edit, rollback
    summary       TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    proposal_id   TEXT,
    approval_id   TEXT,
    actor         TEXT NOT NULL      -- controller | supervisor | approver | simulator
);
CREATE INDEX IF NOT EXISTS audit_wf_ts ON audit (workflow_id, sim_ts);

-- Small durable key/value state for learning: experiment state, flagged DIDs, incidents, credits.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""


def open_db(path):
    # isolation_level=None: we issue BEGIN/COMMIT ourselves, so transaction boundaries are explicit.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is safe against process crashes (kill -9) in WAL mode; only an OS/power loss can drop
    # the last commits, and those events would simply be re-fetched (cursor rolls back with them).
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO cursor (id, last_chunk, last_event_id) VALUES (1, -1, 0)")
    return conn


@contextmanager
def atomic(conn):
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception. Takes the write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---------------------------------------------------------------- cursor

def get_cursor(conn):
    r = conn.execute("SELECT last_chunk, last_event_id FROM cursor WHERE id = 1").fetchone()
    return r["last_chunk"], r["last_event_id"]


# ---------------------------------------------------------------- chunk delta (built by ingest, written here)

@dataclass
class ChunkDelta:
    """Everything one chunk changes. Ingest fills it in memory; commit_chunk writes it in one transaction."""
    chunk: int
    last_event_id: int
    stats: dict = field(default_factory=dict)        # (wf, hour, ver, cohort, dim, value) -> [dials, conn, eng, hal]
    leads: dict = field(default_factory=dict)        # (wf, day, lang, tier, age, value_band) -> n
    campaigns: list = field(default_factory=list)    # (campaign_id, wf, ts, version, leads)
    audits: list = field(default_factory=list)       # dicts for add_audit
    approvals: list = field(default_factory=list)    # (approval_id, status, decided_at, reason)
    applied: list = field(default_factory=list)      # (proposal_id, applied_at) seen in config_changed

    def add_call(self, ev):
        hour = ev["ts"][:13]
        connected = 1 if ev["connected"] else 0
        engaged = 1 if ev.get("engaged") else 0
        halluc = 1 if connected and (ev.get("qa") or {}).get("hallucination_flag") else 0
        for dim in DIMS:
            key = (ev["workflow_id"], hour, ev["config_version"], ev["cohort_id"], dim, ev["variant"][dim])
            c = self.stats.get(key)
            if c is None:
                c = self.stats[key] = [0, 0, 0, 0]
            c[0] += 1
            c[1] += connected
            c[2] += engaged
            c[3] += halluc

    def add_lead(self, ev):
        f = ev["features"]
        key = (ev["workflow_id"], ev["ts"][:10], f["lang"], f["city_tier"], f["age_band"], f["value_band"])
        self.leads[key] = self.leads.get(key, 0) + 1


def commit_chunk(conn, delta):
    """Write a chunk's effects and advance the cursor atomically. Safe to call again after a crash:
    if the cursor is already at or past this chunk, nothing is written. (Ingest also skips individual
    events with event_id <= cursor, so a chunk is never counted twice.)"""
    with atomic(conn):
        last_chunk, last_event_id = get_cursor(conn)
        if delta.chunk <= last_chunk:
            return False  # already committed before a crash/restart
        conn.executemany(
            """INSERT INTO stats (workflow_id, hour, config_version, cohort_id, dim, value,
                                  dials, connected, engaged, halluc)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO UPDATE SET dials = dials + excluded.dials,
                   connected = connected + excluded.connected,
                   engaged = engaged + excluded.engaged, halluc = halluc + excluded.halluc""",
            [(*k, *v) for k, v in delta.stats.items()])
        conn.executemany(
            """INSERT INTO lead_counts (workflow_id, day, lang, city_tier, age_band, value_band, n)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET n = n + excluded.n""",
            [(*k, n) for k, n in delta.leads.items()])
        conn.executemany("INSERT OR IGNORE INTO campaigns VALUES (?,?,?,?,?)", delta.campaigns)
        for a in delta.audits:
            add_audit(conn, **a)
        for approval_id, status, decided_at, reason in delta.approvals:
            set_status_by_approval(conn, approval_id, status, decided_at, reason)
        for proposal_id, applied_at in delta.applied:
            conn.execute("""UPDATE proposals SET status = 'applied', applied_at = COALESCE(applied_at, ?)
                            WHERE proposal_id = ? AND status != 'applied'""", (applied_at, proposal_id))
        conn.execute("UPDATE cursor SET last_chunk = ?, last_event_id = ? WHERE id = 1",
                     (delta.chunk, max(last_event_id, delta.last_event_id)))
    return True


# ---------------------------------------------------------------- stats queries

def sum_stats(conn, workflow_id, dim, since_hour, until_hour=None, cohort_id=None, min_version=None):
    """Totals per option value of `dim` for hours in [since_hour, until_hour).
    Returns {value: {"dials", "connected", "engaged", "halluc"}}."""
    sql = ["SELECT value, SUM(dials) d, SUM(connected) c, SUM(engaged) e, SUM(halluc) h FROM stats",
           "WHERE workflow_id = ? AND dim = ? AND hour >= ?"]
    args = [workflow_id, dim, since_hour]
    if until_hour is not None:
        sql.append("AND hour < ?")
        args.append(until_hour)
    if cohort_id is not None:
        sql.append("AND cohort_id = ?")
        args.append(cohort_id)
    if min_version is not None:
        sql.append("AND config_version >= ?")
        args.append(min_version)
    sql.append("GROUP BY value")
    return {r["value"]: {"dials": r["d"], "connected": r["c"], "engaged": r["e"], "halluc": r["h"]}
            for r in conn.execute(" ".join(sql), args)}


def total_stats(conn, workflow_id, since_hour, until_hour=None, **kw):
    """Workflow totals over a window (sums one dim, since every call appears once per dim)."""
    out = {"dials": 0, "connected": 0, "engaged": 0, "halluc": 0}
    for s in sum_stats(conn, workflow_id, "prompt_version", since_hour, until_hour, **kw).values():
        for k in out:
            out[k] += s[k]
    return out


# ---------------------------------------------------------------- proposals

def insert_proposal(conn, proposal_id, workflow_id, base_version, payload, created_at, status="pending"):
    """INSERT OR IGNORE: inserting the same proposal_id twice (retry after crash) is harmless."""
    conn.execute(
        """INSERT OR IGNORE INTO proposals (proposal_id, workflow_id, status, base_version, payload_json, created_at)
           VALUES (?,?,?,?,?,?)""",
        (proposal_id, workflow_id, status, base_version, json.dumps(payload, sort_keys=True), created_at))


def update_proposal(conn, proposal_id, **fields):
    allowed = {"status", "approval_id", "decided_at", "applied_at", "reason"}
    assert fields and set(fields) <= allowed, fields
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE proposals SET {cols} WHERE proposal_id = ?", (*fields.values(), proposal_id))


def set_status_by_approval(conn, approval_id, status, decided_at, reason=None):
    # Only move forward from pending: a late/replayed event must not undo 'applied'.
    conn.execute(
        """UPDATE proposals SET status = ?, decided_at = ?, reason = COALESCE(?, reason)
           WHERE approval_id = ? AND status = 'pending'""",
        (status, decided_at, reason, approval_id))


def get_proposal(conn, proposal_id):
    return conn.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()


def open_proposals(conn, workflow_id=None):
    """Proposals still in flight: waiting for a decision, or approved but not yet applied."""
    sql = "SELECT * FROM proposals WHERE status IN ('pending', 'approved')"
    if workflow_id is None:
        return conn.execute(sql + " ORDER BY created_at").fetchall()
    return conn.execute(sql + " AND workflow_id = ? ORDER BY created_at", (workflow_id,)).fetchall()


# ---------------------------------------------------------------- audit

def add_audit(conn, sim_ts, kind, summary, actor, workflow_id=None, evidence=None,
              proposal_id=None, approval_id=None):
    conn.execute(
        """INSERT INTO audit (sim_ts, workflow_id, kind, summary, evidence_json, proposal_id, approval_id, actor)
           VALUES (?,?,?,?,?,?,?,?)""",
        (sim_ts, workflow_id, kind, summary, json.dumps(evidence or {}, sort_keys=True),
         proposal_id, approval_id, actor))


# ---------------------------------------------------------------- kv (durable learning state)

def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
    return default if r is None else json.loads(r["value_json"])


def kv_put(conn, key, value):
    conn.execute("INSERT INTO kv (key, value_json) VALUES (?, ?) ON CONFLICT DO UPDATE SET value_json = excluded.value_json",
                 (key, json.dumps(value, sort_keys=True)))
