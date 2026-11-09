"""Governance client (C9): the only code that changes a workflow's config.

Detectors never call the simulator to change anything. They file an *intent* ("move caller_id weight off
D2", "switch prompt to P3"), and once per loop tick governance turns all open intents of a workflow into
ONE change, built on top of the latest config, and routes it:

  auto route      PUT /workflows/{id}/config
                  only if policy allows it (auto_apply.enabled, every changed dim in allowed_dims, shift
                  <= max_shift) AND the change is operational (caller_id / schedule). Customer-facing dims
                  (welcome_message, voice, prompt_version, cohorts) always go to a human (compliance memo),
                  even where policy would let us auto-apply them.

  approval route  POST /approvals -> approver decides -> POST /workflows/{id}/apply

Proposal state machine (status column in `proposals`):

  approval: sending --POST ok--> pending --approved--> approved --apply ok--> applied
                                         --rejected--> rejected   (intents dropped, 24h cool-down)
                                         --expired---> expired    (intents re-opened, rebuilt on fresh data)
                                                       approved --410-------> lapsed  (re-opened)
                                                       approved --409 stale-> stale   (re-opened)
  auto:     auto_sending --PUT ok--> applied
                         --403----> forbidden (intents go to the approval route)
                         --409----> stale     (re-opened)

Crash safety: the proposals row is written BEFORE the HTTP call ("sending"), and proposal_id is a hash of
(workflow, base_version, proposed_config, attempt time). After a crash, reconcile() finds the row and asks the simulator
what happened (GET /approvals, GET config/history) instead of guessing, so nothing is sent or applied twice
and no pending approval is forgotten.
"""
import copy
import hashlib
import json
import logging
from datetime import datetime, timedelta

from controller import store
from controller.client import AutoApplyForbidden, DuplicateProposal, Lapsed, NotApproved, StaleBase

log = logging.getLogger("governance")

ALLOC_DIMS = ("welcome_message", "voice", "caller_id")
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version", "cohorts"}
REJECT_COOLDOWN_HOURS = 24
URGENT_REJECT_COOLDOWN_HOURS = 2   # an open incident can't wait a day; ask again with fresh evidence
# Normal changes leave one request of max_requests_per_24h in reserve for an urgent one (incident fix or a
# stale-base re-proposal), and skip the approver's first hour (stand-ups, morning queue).
QUIET_FIRST_HOURS = 1
# Approver fatigue: even when the policy allows more, at most this many normal requests per workflow per day.
# Batching means a day's changes fit in one or two; a supervisor asked for "two a day, maximum".
MAX_NORMAL_REQUESTS_PER_24H = 2


# ---------------------------------------------------------------- config helpers (pure)

def apply_patch(config, patch):
    """patch = [[path, value], ...] where path is a list of keys, e.g. [["allocations", "default", "voice"], {...}]."""
    out = copy.deepcopy(config)
    for path, value in patch:
        node = out
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return out


def changed_dims(old, new):
    dims = set()
    if old.get("cohorts") != new.get("cohorts"):
        dims.add("cohorts")
    if old.get("prompt_version") != new.get("prompt_version"):
        dims.add("prompt_version")
    if old.get("schedule") != new.get("schedule"):
        dims.add("schedule")
    oa, na = old.get("allocations", {}), new.get("allocations", {})
    for cohort in set(oa) | set(na):
        for dim in ALLOC_DIMS:
            if (oa.get(cohort) or {}).get(dim) != (na.get(cohort) or {}).get(dim):
                dims.add(dim)
    return dims


def _dist(alloc, cohort, dim):
    w = (alloc.get(cohort) or {}).get(dim) or (alloc.get("default") or {}).get(dim) or {}
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def max_shift(old, new):
    """Largest total-variation distance of any cohort's welcome/voice/caller_id distribution."""
    oa, na = old.get("allocations", {}), new.get("allocations", {})
    worst = 0.0
    for cohort in set(oa) | set(na):
        for dim in ALLOC_DIMS:
            p, q = _dist(oa, cohort, dim), _dist(na, cohort, dim)
            worst = max(worst, 0.5 * sum(abs(p.get(k, 0) - q.get(k, 0)) for k in set(p) | set(q)))
    return worst


def config_diff(old, new):
    """Human-readable diff: {dotted.path: {"from": x, "to": y}} for every top-level/allocation leaf changed."""
    diff = {}
    for key in ("prompt_version", "cohorts", "schedule"):
        if old.get(key) != new.get(key):
            diff[key] = {"from": old.get(key), "to": new.get(key)}
    oa, na = old.get("allocations", {}), new.get("allocations", {})
    for cohort in sorted(set(oa) | set(na)):
        for dim in ALLOC_DIMS:
            a, b = (oa.get(cohort) or {}).get(dim), (na.get(cohort) or {}).get(dim)
            if a != b:
                diff[f"allocations.{cohort}.{dim}"] = {"from": a, "to": b}
    return diff


def proposal_id_for(workflow_id, base_version, config, attempt_ts):
    """Deterministic id for one attempt. Retries of the attempt reuse the id stored in the write-ahead row, so
    the simulator dedupes them. attempt_ts makes a deliberate re-proposal of the same config (after expiry)
    a new id instead of a 409 duplicate."""
    canon = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return "p_" + hashlib.sha256(f"{workflow_id}|{base_version}|{canon}|{attempt_ts}".encode()).hexdigest()[:16]


def cohort_of(features, cohorts):
    for c in cohorts:
        if all(str(features.get(f)) in [str(v) for v in vals] for f, vals in c["where"].items()):
            return c["id"]
    return "default"


def affected_leads(conn, workflow_id, config, cohort_ids=None):
    """Leads uploaded on the most recent day we have, that fall in the affected cohorts (None = all).
    An estimate of how many people the change touches tomorrow."""
    day = conn.execute("SELECT MAX(day) d FROM lead_counts WHERE workflow_id = ?", (workflow_id,)).fetchone()["d"]
    if day is None:
        return 0
    total = 0
    for r in conn.execute("SELECT * FROM lead_counts WHERE workflow_id = ? AND day = ?", (workflow_id, day)):
        if cohort_ids is None or cohort_of(dict(r), config.get("cohorts", [])) in cohort_ids:
            total += r["n"]
    return total


def _hours_between(a_ts, b_ts):
    return (datetime.fromisoformat(b_ts) - datetime.fromisoformat(a_ts)).total_seconds() / 3600


def _shift_ts(ts, hours):
    return (datetime.fromisoformat(ts) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------- intents (called by detectors)

def add_intent(conn, now, workflow_id, key, patch, summary, rationale, severity, dry_run,
               evidence=None, cohorts=None, urgent=False):
    """File or refresh a wanted change. One open intent per (workflow, key): a detector calling this every
    tick just refreshes the evidence. Ignored while the same concern is already in a proposal, or within
    the cool-down after a human rejected it. Returns True if the intent is open after the call."""
    intent_id = f"{workflow_id}:{key}"
    until = store.kv_get(conn, f"cooldown:{intent_id}")
    if until and now < until:
        return False
    body = dict(key=key, patch=patch, summary=summary, rationale=rationale,
                severity=max(0.0, min(1.0, float(severity))), dry_run=dry_run,
                evidence=evidence or {}, cohorts=cohorts, urgent=urgent)
    row = conn.execute("SELECT status FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
    if row and row["status"] == "proposed":
        return False
    with store.atomic(conn):
        if row is None or row["status"] in ("done", "dropped"):
            conn.execute("INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,NULL)",
                         (intent_id, workflow_id, "open", json.dumps(body, sort_keys=True), now))
            store.add_audit(conn, now, "intent", f"wants: {summary}", "controller", workflow_id,
                            evidence={"intent_id": intent_id, "severity": body["severity"], "dry_run": dry_run})
        else:
            conn.execute("UPDATE intents SET body_json = ? WHERE intent_id = ?",
                         (json.dumps(body, sort_keys=True), intent_id))
    return True


def withdraw_intent(conn, now, workflow_id, key, reason):
    """A detector no longer wants an open change (e.g. the problem went away)."""
    intent_id = f"{workflow_id}:{key}"
    with store.atomic(conn):
        cur = conn.execute("UPDATE intents SET status = 'dropped' WHERE intent_id = ? AND status = 'open'",
                           (intent_id,))
        if cur.rowcount:
            store.add_audit(conn, now, "intent_dropped", f"withdrawn: {reason}", "controller", workflow_id,
                            evidence={"intent_id": intent_id})


def _intent_dims(body):
    dims = set()
    for path, value in body["patch"]:
        if path[0] == "allocations":
            dims |= {path[2]} if len(path) > 2 else (set(ALLOC_DIMS) if len(path) == 1 else set(value))
        else:
            dims.add(path[0])
    return dims


# ---------------------------------------------------------------- governance engine

class Governance:
    def __init__(self, conn, client):
        self.conn, self.client = conn, client
        self._wf_cache = {}

    def policy(self, workflow_id):
        if workflow_id not in self._wf_cache:
            self._wf_cache[workflow_id] = self.client.workflow(workflow_id)
        return self._wf_cache[workflow_id]

    # ---- one tick

    def tick(self, now):
        """Reconcile everything in flight, then propose for workflows that have open intents."""
        self._settle_decided_by_events(now)
        for p in store.open_proposals(self.conn):
            self._advance_proposal(p, now)
        wids = [r["workflow_id"] for r in self.conn.execute(
            "SELECT DISTINCT workflow_id FROM intents WHERE status = 'open'")]
        for wid in wids:
            if not store.open_proposals(self.conn, wid):  # one change in flight per workflow
                self._propose(wid, now)

    # ---- in-flight proposals

    def _advance_proposal(self, p, now):
        status, wid, pid, approval_id = p["status"], p["workflow_id"], p["proposal_id"], p["approval_id"]
        reasons = p["reason"]

        if status == "sending":  # we crashed (or failed) between writing the row and hearing back
            server = next((a for a in self.client.approvals(workflow_id=wid) if a["proposal_id"] == pid), None)
            if server is None:  # the simulator never got it: it didn't happen. Rebuild on fresh data.
                return self._close(pid, wid, now, "abandoned", "request never reached the approver; rebuilding",
                                   reopen=True)
            self._record_submitted(pid, server, now)
            status, approval_id = server["status"], server["approval_id"]

        if status == "auto_sending":
            history = self.client.get(f"/workflows/{wid}/config/history")
            if any(h.get("proposal_id") == pid for h in history):
                return self._finish(pid, wid, now, "applied", "auto-apply confirmed from config history",
                                    "auto_applied")
            return self._close(pid, wid, now, "abandoned", "auto-apply never reached the simulator; rebuilding",
                               reopen=True)

        if status == "pending":
            server = self.client.approval(approval_id)
            status = server["status"]
            if status == "pending":
                return
            reasons = "; ".join(map(str, server.get("reasons") or [])) or None
            with store.atomic(self.conn):
                store.set_status_by_approval(self.conn, approval_id, status, now, reasons)

        if status == "approved":
            return self._apply(wid, pid, approval_id, now)
        if status == "rejected":
            return self._close(pid, wid, now, "rejected", f"approver rejected ({reasons or 'no reason given'}); "
                               f"not re-asking for {REJECT_COOLDOWN_HOURS}h", reopen=False, cooldown=True)
        if status == "expired":
            return self._close(pid, wid, now, "expired", "no decision before expiry; will re-propose on fresh data",
                               reopen=True)

    def _settle_decided_by_events(self, now):
        """Ingest may already have moved a proposal to rejected/expired/applied from an event. Finish the
        bookkeeping for its intents here, so they never get stuck in 'proposed'."""
        rows = self.conn.execute(
            """SELECT DISTINCT p.* FROM proposals p JOIN intents i ON i.proposal_id = p.proposal_id
               WHERE i.status = 'proposed' AND p.status IN ('rejected', 'expired', 'applied')""").fetchall()
        for p in rows:
            if p["status"] == "applied":
                with store.atomic(self.conn):
                    self.conn.execute("UPDATE intents SET status = 'done' WHERE proposal_id = ?", (p["proposal_id"],))
            elif p["status"] == "rejected":
                self._close(p["proposal_id"], p["workflow_id"], now, "rejected",
                            f"approver rejected ({p['reason'] or 'no reason given'}); not re-asking for "
                            f"{REJECT_COOLDOWN_HOURS}h", reopen=False, cooldown=True)
            else:
                self._close(p["proposal_id"], p["workflow_id"], now, "expired",
                            "no decision before expiry; will re-propose on fresh data", reopen=True)

    def _apply(self, wid, pid, approval_id, now):
        try:
            res = self.client.apply(wid, approval_id)
        except StaleBase as e:
            return self._close(pid, wid, now, "stale",
                               f"config changed since proposal (now v{(e.detail or {}).get('current_version')}); "
                               "re-proposing on top of it", reopen=True)
        except Lapsed:
            return self._close(pid, wid, now, "lapsed", "approved but apply window passed; re-proposing",
                               reopen=True)
        except NotApproved as e:
            if (e.detail or {}).get("status") == "applied":  # we applied it before a crash
                return self._finish(pid, wid, now, "applied", "already applied (confirmed by simulator)", "applied")
            raise
        self._finish(pid, wid, now, "applied", f"applied approved change -> v{res.get('version')}", "applied",
                     approval_id=approval_id)

    # ---- building a new proposal

    def _propose(self, wid, now):
        wf = self.policy(wid)
        gov = wf["governance"]
        cur = self.client.config(wid)
        base, config = cur["version"], cur["config"]
        intents = self._live_intents(wid, now)
        if not intents:
            return

        auto_ok = gov["auto_apply"]["enabled"]
        allowed = set(gov["auto_apply"]["allowed_dims"])
        auto = [i for i in intents if auto_ok and not (_intent_dims(i) & CUSTOMER_FACING)
                and _intent_dims(i) <= allowed and not i.get("needs_approval")]
        if auto:
            new = self._build(config, auto)
            if new != config and max_shift(config, new) <= gov["auto_apply"]["max_shift"] + 1e-9:
                pid = proposal_id_for(wid, base, new, now)
                payload = dict(proposal_id=pid, base_version=base, proposed_config=new,
                               rationale=" | ".join(i["rationale"] for i in auto),
                               evidence={"intents": [i["intent_id"] for i in auto],
                                         "dry_run": {i["key"]: i["dry_run"] for i in auto}})
                self._write_ahead(wid, pid, base, payload, now, auto, route="auto")
                return self._send_auto(wid, pid, payload, now)
            for i in auto:  # shift too big for auto-apply: ask a human instead
                i["needs_approval"] = True

        if not self._may_ask_approver(wid, gov, now, urgent=any(i["urgent"] for i in intents)):
            return
        new = self._build(config, intents)
        if new == config:
            for i in intents:
                self._set_intent(i["intent_id"], "done", None)
            return
        payload = self._approval_payload(wid, base, config, new, intents, gov, now)
        self._write_ahead(wid, payload["proposal_id"], base, payload, now, intents, route="approval")
        self._send_approval(wid, payload["proposal_id"], payload, now)

    def _live_intents(self, wid, now):
        """Open intents, minus any a supervisor has since overridden (we never silently undo their edit)."""
        live = []
        for r in self.conn.execute("SELECT * FROM intents WHERE workflow_id = ? AND status = 'open' ORDER BY created_at",
                                   (wid,)):
            body = json.loads(r["body_json"])
            body.update(intent_id=r["intent_id"], created_at=r["created_at"])
            edits = self.conn.execute(
                "SELECT sim_ts, evidence_json FROM audit WHERE workflow_id = ? AND kind = 'supervisor_edit' AND sim_ts > ?",
                (wid, r["created_at"])).fetchall()
            touched = set().union(*[set(json.loads(e["evidence_json"]).get("changed_dims") or []) for e in edits]) \
                if edits else set()
            if touched & _intent_dims(body):
                with store.atomic(self.conn):
                    self.conn.execute("UPDATE intents SET status = 'dropped' WHERE intent_id = ?", (r["intent_id"],))
                    store.add_audit(self.conn, now, "intent_dropped",
                                    f"supervisor edited {', '.join(sorted(touched & _intent_dims(body)))} after we "
                                    f"planned '{body['summary']}'; respecting their edit", "controller", wid,
                                    evidence={"intent_id": r["intent_id"]})
                continue
            live.append(body)
        return live

    @staticmethod
    def _build(config, intents):
        new = config
        for i in intents:
            new = apply_patch(new, i["patch"])
        return new

    def _may_ask_approver(self, wid, gov, now, urgent):
        start, end = gov["approver_hours"]
        hour = int(now[11:13])
        if not urgent:
            start += QUIET_FIRST_HOURS
        if not start <= hour < end:
            return False
        # leave the approver enough of their day to answer (average latency), unless urgent
        lo, hi = gov["approver_latency_minutes"]
        if not urgent and hour + (lo + hi) / 2 / 60 > end:
            return False
        sent = self.conn.execute(
            """SELECT COUNT(*) n FROM proposals WHERE workflow_id = ? AND route = 'approval'
               AND status != 'abandoned' AND created_at > ?""",
            (wid, _shift_ts(now, -24))).fetchone()["n"]
        cap = gov["max_requests_per_24h"]
        return sent < (cap if urgent else max(1, min(cap - 1, MAX_NORMAL_REQUESTS_PER_24H)))

    def _approval_payload(self, wid, base, config, new, intents, gov, now):
        all_cohorts = None if any(i.get("cohorts") is None for i in intents) else \
            sorted({c for i in intents for c in i["cohorts"]})
        leads = affected_leads(self.conn, wid, config, all_cohorts)
        total = affected_leads(self.conn, wid, config, None) or 1
        share = min(1.0, leads / total)
        severity = max(i["severity"] for i in intents)
        # impact_score: documented in SUBMISSION.md. Half how bad/valuable the issue is (detector's severity
        # in [0,1]), half how much of the workflow's traffic it touches.
        impact = round(0.5 * severity + 0.5 * share, 3)
        evidence = {}
        for i in intents:
            evidence.update(i.get("evidence") or {})
        n = len(intents)
        return dict(
            proposal_id=proposal_id_for(wid, base, new, now), workflow_id=wid, base_version=base, proposed_config=new,
            change_summary=(intents[0]["summary"] if n == 1 else f"{n} changes: " + "; ".join(i["summary"] for i in intents)),
            diff=config_diff(config, new),
            affected_leads=int(leads),
            dry_run={i["key"]: i["dry_run"] for i in intents},
            impact_score=impact,
            rationale=" | ".join(i["rationale"] for i in intents)
                      + (f" (batched {n} changes into one request)" if n > 1 else ""),
            evidence=evidence,
            # Full policy TTL: a slow approver may finish tomorrow morning. If the config moved meanwhile,
            # apply returns 409 stale and we re-propose on fresh data.
            expires_in_hours=gov["proposal_ttl_hours"],
        )

    # ---- sending

    def _write_ahead(self, wid, pid, base, payload, now, intents, route):
        with store.atomic(self.conn):
            store.insert_proposal(self.conn, pid, wid, base, payload, now,
                                  status="auto_sending" if route == "auto" else "sending", route=route)
            for i in intents:
                self._set_intent(i["intent_id"], "proposed", pid, tx=False)

    def _send_approval(self, wid, pid, payload, now):
        try:
            res = self.client.request_approval(payload)
        except DuplicateProposal:  # sent before a crash; the server has it
            res = next(a for a in self.client.approvals(workflow_id=wid) if a["proposal_id"] == pid)
        except StaleBase:
            return self._close(pid, wid, now, "stale", "config changed while proposing; rebuilding", reopen=True)
        self._record_submitted(pid, res, now, payload)

    def _record_submitted(self, pid, server, now, payload=None):
        with store.atomic(self.conn):
            store.update_proposal(self.conn, pid, approval_id=server["approval_id"], status=server["status"])
            if payload:
                store.add_audit(self.conn, now, "propose", payload["change_summary"], "controller",
                                server["workflow_id"], proposal_id=pid, approval_id=server["approval_id"],
                                evidence={k: payload[k] for k in ("diff", "affected_leads", "dry_run",
                                                                  "impact_score", "rationale", "evidence",
                                                                  "base_version")})

    def _send_auto(self, wid, pid, payload, now):
        try:
            res = self.client.auto_apply(wid, payload)
        except DuplicateProposal:
            return  # already sent; next reconcile confirms it from config history
        except StaleBase:
            return self._close(pid, wid, now, "stale", "config changed before auto-apply; rebuilding", reopen=True)
        except AutoApplyForbidden as e:
            with store.atomic(self.conn):
                store.update_proposal(self.conn, pid, status="forbidden", decided_at=now, reason=str(e.detail))
                for r in self.conn.execute("SELECT intent_id, body_json FROM intents WHERE proposal_id = ?", (pid,)):
                    body = json.loads(r["body_json"])
                    body["needs_approval"] = True
                    self.conn.execute("UPDATE intents SET status = 'open', proposal_id = NULL, body_json = ? "
                                      "WHERE intent_id = ?", (json.dumps(body, sort_keys=True), r["intent_id"]))
                store.add_audit(self.conn, now, "auto_forbidden", f"auto-apply refused: {e.detail}; will ask approver",
                                "controller", wid, proposal_id=pid)
            return
        self._finish(pid, wid, now, "applied",
                     f"auto-applied {', '.join(res.get('changed_dims') or [])} -> v{res.get('version')} "
                     f"({payload['rationale']})", "auto_applied", evidence=payload["evidence"])

    # ---- closing

    def _finish(self, pid, wid, now, status, summary, kind, approval_id=None, evidence=None):
        with store.atomic(self.conn):
            store.update_proposal(self.conn, pid, status=status, applied_at=now)
            self.conn.execute("UPDATE intents SET status = 'done' WHERE proposal_id = ?", (pid,))
            store.add_audit(self.conn, now, kind, summary, "controller", wid, proposal_id=pid,
                            approval_id=approval_id, evidence=evidence)

    def _close(self, pid, wid, now, status, summary, reopen, cooldown=False):
        with store.atomic(self.conn):
            store.update_proposal(self.conn, pid, status=status, decided_at=now, reason=summary)
            for r in self.conn.execute("SELECT intent_id, body_json FROM intents WHERE proposal_id = ?",
                                       (pid,)).fetchall():
                if reopen:
                    self.conn.execute("UPDATE intents SET status = 'open', proposal_id = NULL WHERE intent_id = ?",
                                      (r["intent_id"],))
                else:
                    self.conn.execute("UPDATE intents SET status = 'dropped' WHERE intent_id = ?", (r["intent_id"],))
                if cooldown:
                    urgent = json.loads(r["body_json"]).get("urgent")
                    hours = URGENT_REJECT_COOLDOWN_HOURS if urgent else REJECT_COOLDOWN_HOURS
                    store.kv_put(self.conn, f"cooldown:{r['intent_id']}", _shift_ts(now, hours))
            store.add_audit(self.conn, now, status, summary, "controller", wid, proposal_id=pid)

    def _set_intent(self, intent_id, status, pid, tx=True):
        sql, args = "UPDATE intents SET status = ?, proposal_id = ? WHERE intent_id = ?", (status, pid, intent_id)
        if tx:
            with store.atomic(self.conn):
                self.conn.execute(sql, args)
        else:
            self.conn.execute(sql, args)
