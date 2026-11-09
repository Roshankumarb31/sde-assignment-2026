"""Answer questions from the controller's own store (never the simulator's logs).

  python -m controller.explain --db state.db --workflow ks_cancel --at "D4 11:00"
      what config was live at that moment, the change nearest before it: what, why, who approved,
      evidence, and what happened next (KPIs 24 h before vs after, follow-up decisions)

  python -m controller.explain --db state.db --view
      one screen for a supervisor: per workflow what's live, open incidents, pending asks, changes
      in the last 24 h, alerts; plus the loop's own health

Read-only: safe to run while the controller is running (SQLite WAL).
"""
import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta

from controller import store
from controller.stats import rate

CHANGE_KINDS = ("config_applied", "supervisor_edit")


def connect(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_at(conn, at):
    """'D3 14:00' (days since D0) or an ISO timestamp -> ISO timestamp in sim time."""
    m = re.fullmatch(r"D(\d+)\s+(\d{1,2}):(\d{2})", at.strip())
    if not m:
        return datetime.fromisoformat(at).isoformat()
    d0 = datetime.fromisoformat(store.kv_get(conn, "world")["d0"])
    return (d0 + timedelta(days=int(m[1]), hours=int(m[2]), minutes=int(m[3]))).isoformat()


def label(conn, ts):
    """ISO -> 'D3 14:00' for humans."""
    d0 = datetime.fromisoformat(store.kv_get(conn, "world")["d0"])
    delta = datetime.fromisoformat(ts) - d0
    day, rest = divmod(int(delta.total_seconds()) // 60, 1440)
    return f"D{day} {rest // 60:02d}:{rest % 60:02d}"


def kpis(conn, wid, since, until):
    t = store.total_stats(conn, wid, since[:13], until[:13])
    return {"dials": t["dials"], "pickup": round(rate(t["connected"], t["dials"]), 4),
            "engagement": round(rate(t["engaged"], t["connected"]), 4),
            "hallucination": round(rate(t["halluc"], t["connected"]), 4)}


def shift(ts, hours):
    return (datetime.fromisoformat(ts) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------- "why did X change at T"

def explain_at(conn, wid, at):
    t = parse_at(conn, at)
    out = [f"# {wid} at {label(conn, t)} ({t})"]
    change = conn.execute(
        f"SELECT * FROM audit WHERE workflow_id = ? AND kind IN {CHANGE_KINDS} AND sim_ts <= ? ORDER BY sim_ts DESC, id DESC",
        (wid, t)).fetchone()
    if change is None:
        out.append("No config change before this time: the business default config (v1) was live.")
        return "\n".join(out)
    ev = json.loads(change["evidence_json"])
    out.append(f"Live config: v{ev.get('version')} since {label(conn, change['sim_ts'])} "
               f"(changed {', '.join(ev.get('changed_dims') or [])} by {change['actor']})")

    if change["kind"] == "supervisor_edit":
        out.append("\n## Why\nA client supervisor edited the config directly (outside governance). "
                   "We record it and never overwrite it silently.")
    else:
        p = store.get_proposal(conn, change["proposal_id"]) if change["proposal_id"] else None
        if p:
            pay = json.loads(p["payload_json"])
            out.append(f"\n## What and why (proposal {p['proposal_id']}, route {p['route']})")
            if pay.get("change_summary"):
                out.append(f"Summary: {pay['change_summary']}")
            out.append(f"Rationale: {pay.get('rationale')}")
            if pay.get("diff"):
                out.append("Diff: " + json.dumps(pay["diff"]))
            for k in ("affected_leads", "impact_score", "evidence", "dry_run"):
                if pay.get(k) not in (None, {}, []):
                    out.append(f"{k}: {json.dumps(pay[k])}")
            out.append(f"\n## Who approved\nproposed {label(conn, p['created_at'])} against v{p['base_version']}")
            if p["route"] == "auto":
                out.append("auto-applied within the workflow's auto_apply policy (operational change, no human needed)")
            for d in conn.execute("SELECT * FROM audit WHERE approval_id = ? AND kind = 'approval_decided'",
                                  (p["approval_id"],)):
                out.append(f"{label(conn, d['sim_ts'])} approver ({p['approval_id']}): {d['summary']}")
            out.append(f"applied {label(conn, p['applied_at'])}" if p["applied_at"] else "")
        # detector context: the decisions that led to this change
        for r in conn.execute(
                """SELECT * FROM audit WHERE workflow_id = ? AND actor = 'controller' AND sim_ts BETWEEN ? AND ?
                   AND kind LIKE 'incident_%' ORDER BY id""", (wid, shift(change["sim_ts"], -48), change["sim_ts"])):
            out.append(f"  context {label(conn, r['sim_ts'])} {r['kind']}: {r['summary']}")

    before = kpis(conn, wid, shift(change["sim_ts"], -24), change["sim_ts"])
    after = kpis(conn, wid, change["sim_ts"], shift(change["sim_ts"], 24))
    out.append("\n## What happened next (24 h before vs 24 h after; pickup and engagement kept separate)")
    out.append(f"{'':14}{'pickup':>10}{'engagement':>12}{'hallucination':>15}{'dials':>8}")
    for name, k in (("before", before), ("after", after)):
        out.append(f"{name:14}{k['pickup']:>10.1%}{k['engagement']:>12.1%}{k['hallucination']:>15.2%}{k['dials']:>8}")
    for r in conn.execute("SELECT * FROM audit WHERE workflow_id = ? AND sim_ts > ? AND sim_ts <= ? ORDER BY id",
                          (wid, change["sim_ts"], shift(change["sim_ts"], 48))):
        if r["kind"] not in ("intent", "intent_dropped", "validated"):
            out.append(f"  {label(conn, r['sim_ts'])} {r['kind']}: {r['summary']}")
    return "\n".join(x for x in out if x != "")


# ---------------------------------------------------------------- supervisor view

def view(conn):
    last = conn.execute("SELECT MAX(sim_ts) t FROM audit").fetchone()["t"]
    health = store.kv_get(conn, "health", {})
    now = health.get("ts") or last
    out = [f"# Controller view at {label(conn, now)}   "
           f"(ingest lag {health.get('lag', '?')} events, credits left {health.get('credits', '?')})"]
    wids = [r[0] for r in conn.execute("SELECT DISTINCT workflow_id FROM stats ORDER BY 1")]
    for wid in wids:
        k = kpis(conn, wid, shift(now, -24), shift(now, 1))
        out.append(f"\n## {wid}   last 24 h: pickup {k['pickup']:.1%} · engagement {k['engagement']:.1%} · "
                   f"hallucination {k['hallucination']:.2%}")
        inc = store.kv_get(conn, f"incident:{wid}")
        out.append(f"  Incident: {inc['phase']} (bad prompt {inc['bad_prompt']}, fix {inc.get('chosen', '?')})"
                   if inc else "  Incident: none open")
        out.append("  Testing: no experiment running")
        asks = store.open_proposals(conn, wid)
        for p in asks:
            pay = json.loads(p["payload_json"])
            out.append(f"  Waiting on you: {pay.get('change_summary') or pay.get('rationale')} "
                       f"[{p['status']}, sent {label(conn, p['created_at'])}, impact {pay.get('impact_score', '-')}]")
        if not asks:
            out.append("  Waiting on you: nothing")
        changes = conn.execute(
            f"SELECT * FROM audit WHERE workflow_id = ? AND kind IN {CHANGE_KINDS} AND sim_ts > ? ORDER BY id",
            (wid, shift(now, -24))).fetchall()
        for c in changes:
            out.append(f"  Changed {label(conn, c['sim_ts'])}: {c['summary']}")
        if not changes:
            out.append("  Changed in last 24 h: nothing")
        for a in conn.execute("SELECT * FROM audit WHERE workflow_id = ? AND summary LIKE 'ALERT%' AND sim_ts > ?",
                              (wid, shift(now, -24))):
            out.append(f"  ALERT {label(conn, a['sim_ts'])}: {a['summary']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--workflow")
    ap.add_argument("--at", help='"D3 14:00" or ISO time')
    ap.add_argument("--view", action="store_true")
    a = ap.parse_args()
    conn = connect(a.db)
    if a.view:
        print(view(conn))
    elif a.workflow and a.at:
        print(explain_at(conn, a.workflow, a.at))
    else:
        ap.error("use --view, or --workflow X --at 'D3 14:00'")


if __name__ == "__main__":
    main()
