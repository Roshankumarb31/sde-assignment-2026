"""Governance state machine against a small in-memory fake of the simulator's governance API."""
import copy

import pytest

from controller import governance as g
from controller import store
from controller.client import AutoApplyForbidden, DuplicateProposal, NotApproved, StaleBase

BASE_CFG = {"cohorts": [], "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
            "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                        "caller_id": {"D1": 0.5, "D2": 0.5}}}}


def gov_policy(auto=True, allowed=("caller_id", "schedule"), max_shift=0.5, cap=3, hours=(10, 19)):
    return {"auto_apply": {"enabled": auto, "allowed_dims": list(allowed), "max_shift": max_shift},
            "approver_hours": list(hours), "approver_latency_minutes": [30, 240], "proposal_ttl_hours": 24,
            "apply_window_hours": 2, "max_requests_per_24h": cap}


class FakeSim:
    def __init__(self, policy):
        self.policy, self.version, self.cfg = policy, 1, copy.deepcopy(BASE_CFG)
        self.apprs, self.history, self.posts, self.puts = {}, [], 0, 0

    def workflow(self, wid):
        return {"workflow_id": wid, "governance": self.policy}

    def config(self, wid):
        return {"version": self.version, "config": copy.deepcopy(self.cfg)}

    def _bump(self, cfg, pid):
        self.version += 1
        self.cfg = copy.deepcopy(cfg)
        self.history.append({"version": self.version, "proposal_id": pid})

    def supervisor_edit(self, cfg):
        self._bump(cfg, None)

    def auto_apply(self, wid, body):
        self.puts += 1
        if any(h["proposal_id"] == body["proposal_id"] for h in self.history):
            raise DuplicateProposal(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.version:
            raise StaleBase(409, "stale_base_version", {"current_version": self.version})
        dims = g.changed_dims(self.cfg, body["proposed_config"])
        if not self.policy["auto_apply"]["enabled"] or not dims <= set(self.policy["auto_apply"]["allowed_dims"]):
            raise AutoApplyForbidden(403, "auto_apply_not_permitted", ["dims"])
        self._bump(body["proposed_config"], body["proposal_id"])
        return {"version": self.version, "changed_dims": sorted(dims)}

    def request_approval(self, body):
        self.posts += 1
        if any(a["proposal_id"] == body["proposal_id"] for a in self.apprs.values()):
            raise DuplicateProposal(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.version:
            raise StaleBase(409, "stale_base_version", {"current_version": self.version})
        aid = f"apr_{len(self.apprs) + 1:05d}"
        self.apprs[aid] = dict(approval_id=aid, proposal_id=body["proposal_id"], workflow_id=body["workflow_id"],
                               status="pending", body=body, reasons=[])
        return {k: v for k, v in self.apprs[aid].items() if k != "body"}

    def approvals(self, workflow_id=None, status=None):
        return [{k: v for k, v in a.items() if k != "body"} for a in self.apprs.values()]

    def approval(self, aid):
        return {k: v for k, v in self.apprs[aid].items() if k != "body"}

    def decide(self, status, reasons=()):
        a = list(self.apprs.values())[-1]
        a["status"], a["reasons"] = status, list(reasons)

    def apply(self, wid, aid):
        a = self.apprs[aid]
        if a["status"] != "approved":
            raise NotApproved(409, "not_approved", {"status": a["status"]})
        if a["body"]["base_version"] != self.version:
            raise StaleBase(409, "stale_base_version", {"current_version": self.version})
        self._bump(a["body"]["proposed_config"], a["proposal_id"])
        a["status"] = "applied"
        return {"version": self.version}

    def get(self, path):
        return self.history


T = lambda h: f"2027-01-05T{h:02d}:00:00+05:30"  # noqa: E731


@pytest.fixture
def conn(tmp_path):
    c = store.open_db(tmp_path / "s.db")
    # one day of leads so affected_leads has something to count
    c.execute("INSERT INTO lead_counts VALUES ('wf','2027-01-05','hi','1','60+','mid',300)")
    c.execute("INSERT INTO lead_counts VALUES ('wf','2027-01-05','ta','2','18-30','low',700)")
    return c


def did_intent(conn, now, weights=None, urgent=False):
    return g.add_intent(conn, now, "wf", "caller_id", [[["allocations", "default", "caller_id"],
                                                       weights or {"D1": 0.9, "D2": 0.1}]],
                        "move caller-ID traffic off D2", "D2 pickup 40% below other DIDs", 0.6,
                        {"D2_pickup": 0.08, "others": 0.14}, urgent=urgent)


def voice_intent(conn, now):
    return g.add_intent(conn, now, "wf", "voice", [[["allocations", "default", "voice"], {"V1": 0.5, "V2": 0.5}]],
                        "test voice V2 against V1", "V2 untested", 0.3, {"note": "no history for V2"})


def statuses(conn):
    return [r["status"] for r in conn.execute("SELECT status FROM proposals ORDER BY created_at, rowid")]


def test_operational_change_is_auto_applied(conn):
    sim = FakeSim(gov_policy())
    did_intent(conn, T(3))
    g.Governance(conn, sim).tick(T(3))           # auto-apply doesn't wait for approver hours
    assert sim.puts == 1 and sim.posts == 0 and sim.version == 2
    assert statuses(conn) == ["applied"]
    assert conn.execute("SELECT status FROM intents").fetchone()["status"] == "done"


def test_customer_facing_change_goes_to_approver_even_if_policy_allows_auto(conn):
    sim = FakeSim(gov_policy(allowed=("caller_id", "schedule", "voice", "welcome_message")))
    voice_intent(conn, T(12))
    g.Governance(conn, sim).tick(T(12))
    assert sim.puts == 0 and sim.posts == 1


def test_big_shift_falls_back_to_approval(conn):
    sim = FakeSim(gov_policy(max_shift=0.2))
    did_intent(conn, T(12), {"D1": 1.0, "D2": 0.0})   # TV distance 0.5 > 0.2
    g.Governance(conn, sim).tick(T(12))
    assert sim.puts == 0 and sim.posts == 1


def test_changes_are_batched_into_one_request(conn):
    sim = FakeSim(gov_policy(auto=False))
    did_intent(conn, T(12))
    voice_intent(conn, T(12))
    g.Governance(conn, sim).tick(T(12))
    assert sim.posts == 1
    body = sim.apprs["apr_00001"]["body"]
    assert body["change_summary"].startswith("2 changes")
    assert set(body["diff"]) == {"allocations.default.caller_id", "allocations.default.voice"}
    assert 0 <= body["impact_score"] <= 1 and body["affected_leads"] == 1000
    assert body["dry_run"] and body["rationale"]


@pytest.mark.parametrize("hour,urgent,sent", [(8, False, 0), (10, False, 0), (10, True, 1), (11, False, 1),
                                             (17, False, 0), (19, True, 0)])
def test_only_asks_inside_approver_hours(conn, hour, urgent, sent):
    sim = FakeSim(gov_policy(auto=False))
    did_intent(conn, T(hour), urgent=urgent)
    g.Governance(conn, sim).tick(T(hour))
    assert sim.posts == sent


def test_request_budget_keeps_one_in_reserve(conn):
    sim = FakeSim(gov_policy(auto=False, cap=2))
    gv = g.Governance(conn, sim)
    did_intent(conn, T(11))
    gv.tick(T(11))
    sim.decide("rejected")
    gv.tick(T(12))                                # rejected -> dropped, cool-down
    voice_intent(conn, T(12))
    gv.tick(T(12))
    assert sim.posts == 1                         # normal change may not use the last request
    g.add_intent(conn, T(13), "wf", "prompt", [[["prompt_version"], "P3"]], "fix prompt", "incident", 1.0,
                 {"x": 1}, urgent=True)
    gv.tick(T(13))
    assert sim.posts == 2                         # urgent one may


def test_approved_is_applied_and_stale_is_reproposed_on_fresh_config(conn):
    sim = FakeSim(gov_policy(auto=False))
    gv = g.Governance(conn, sim)
    voice_intent(conn, T(11))
    gv.tick(T(11))
    sim.decide("approved")
    sim.supervisor_edit({**sim.cfg, "schedule": {"hour_weights": {"default": {"10": 1}}, "lead_preferred_hour": {}}})
    gv.tick(T(12))                                # apply -> 409 stale -> re-opened -> re-proposed same tick
    assert statuses(conn) == ["stale", "pending"]
    new = sim.apprs["apr_00002"]["body"]          # built on top of the supervisor's version
    assert new["base_version"] == 2 and new["proposed_config"]["schedule"]["hour_weights"] == {"default": {"10": 1}}
    sim.decide("approved")
    gv.tick(T(14))
    assert statuses(conn) == ["stale", "applied"] and sim.cfg["allocations"]["default"]["voice"] == {"V1": 0.5, "V2": 0.5}


def test_supervisor_edit_on_same_dim_drops_our_intent(conn):
    sim = FakeSim(gov_policy(auto=False))
    voice_intent(conn, T(11))
    store.add_audit(conn, T(11)[:14] + "30:00+05:30", "supervisor_edit", "supervisor changed voice", "supervisor",
                    "wf", evidence={"version": 2, "changed_dims": ["voice"]})
    g.Governance(conn, sim).tick(T(12))
    assert sim.posts == 0
    assert conn.execute("SELECT status FROM intents").fetchone()["status"] == "dropped"


def test_rejected_starts_cooldown(conn):
    sim = FakeSim(gov_policy(auto=False))
    gv = g.Governance(conn, sim)
    voice_intent(conn, T(11))
    gv.tick(T(11))
    sim.decide("rejected", ["judgement call"])
    gv.tick(T(12))
    assert statuses(conn) == ["rejected"]
    assert voice_intent(conn, T(13)) is False     # cool-down: we don't nag the approver


def test_expired_is_reproposed_with_a_new_id(conn):
    sim = FakeSim(gov_policy(auto=False))
    gv = g.Governance(conn, sim)
    voice_intent(conn, T(11))
    gv.tick(T(11))
    sim.decide("expired")
    gv.tick(T(12))                                # same base, same config, new attempt -> new id, no 409
    assert statuses(conn) == ["expired", "pending"] and sim.posts == 2


def test_crash_after_post_reaches_server_does_not_resend(conn):
    sim = FakeSim(gov_policy(auto=False))
    voice_intent(conn, T(11))
    real_record = g.Governance._record_submitted
    g.Governance._record_submitted = lambda *a, **k: (_ for _ in ()).throw(SystemExit("kill -9"))
    try:
        with pytest.raises(SystemExit):
            g.Governance(conn, sim).tick(T(11))
    finally:
        g.Governance._record_submitted = real_record
    assert statuses(conn) == ["sending"] and sim.posts == 1
    g.Governance(conn, sim).tick(T(12))           # restart: found on the server, not sent again
    assert sim.posts == 1 and statuses(conn) == ["pending"]
    assert store.get_proposal(conn, sim.apprs["apr_00001"]["proposal_id"])["approval_id"] == "apr_00001"


def test_crash_before_post_rebuilds(conn):
    sim = FakeSim(gov_policy(auto=False))
    voice_intent(conn, T(11))
    real_send = g.Governance._send_approval
    g.Governance._send_approval = lambda *a, **k: (_ for _ in ()).throw(SystemExit("kill -9"))
    try:
        with pytest.raises(SystemExit):
            g.Governance(conn, sim).tick(T(11))
    finally:
        g.Governance._send_approval = real_send
    g.Governance(conn, sim).tick(T(12))           # never reached server -> abandoned, then re-proposed fresh
    assert statuses(conn) == ["abandoned", "pending"] and sim.posts == 1


def test_crash_after_apply_does_not_double_apply(conn):
    sim = FakeSim(gov_policy(auto=False))
    gv = g.Governance(conn, sim)
    voice_intent(conn, T(11))
    gv.tick(T(11))
    sim.decide("approved")
    sim.apply("wf", "apr_00001")                  # applied, then we "crashed" before recording it
    gv.tick(T(12))
    assert statuses(conn) == ["applied"] and sim.version == 2


def test_tv_distance_and_dims():
    new = g.apply_patch(BASE_CFG, [[["allocations", "default", "caller_id"], {"D1": 1.0}]])
    assert g.changed_dims(BASE_CFG, new) == {"caller_id"}
    assert g.max_shift(BASE_CFG, new) == pytest.approx(0.5)
