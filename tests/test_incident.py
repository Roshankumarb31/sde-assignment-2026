"""Incident flow: thresholds, deterministic choice, credit safety across restarts, verify/revert, alerts."""
import pytest

from controller import store
from controller.client import CreditsExhausted
from controller.incident import IncidentDetector

D0 = "2027-01-04T00:00:00+05:30"
NOW = "2027-01-07T19:00:00+05:30"
GOV = {"min_validation_pass_rate": 0.97}
OBJECTIVE = {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}


class FakeLLM:
    def __init__(self, pass_rates, credits=200):
        self.pass_rates, self.credits = pass_rates, credits
        self.suggest_calls, self.validate_calls, self.prompt = 0, [], "P2"

    def workflows(self):
        return [{"workflow_id": "wf", "governance": GOV, "objective": OBJECTIVE}]

    def config(self, wid):
        return {"config": {"prompt_version": self.prompt}}

    def suggest_fixes(self, wid, problem):
        self.suggest_calls += 1
        self.credits -= 3
        return {"suggestions": [{"prompt_version": p, "summary": p} for p in ("P3", "P1", "P4", "P2")]}

    def validate(self, wid, pv, n):
        if self.credits < 4:
            raise CreditsExhausted(402, "out_of_credits", {"remaining": self.credits})
        self.credits -= 4
        self.validate_calls.append(pv)
        return {"validation_run_id": f"val_{pv}", "pass_rate": self.pass_rates[pv], "n": n}


def add_calls(conn, hour, prompt, connected, halluc, engaged):
    conn.execute("INSERT INTO stats VALUES ('wf', ?, 1, 'default', 'prompt_version', ?, ?, ?, ?, ?)",
                 (hour, prompt, connected * 4, connected, engaged, halluc))


@pytest.fixture
def conn(tmp_path):
    c = store.open_db(tmp_path / "s.db")
    store.kv_put(c, "world", {"scenario": "t", "d0": D0})
    add_calls(c, "2027-01-01T10", "P1", 5000, 70, 2500)        # history: 1.4% hallucination, 50% engaged
    return c


def det(conn, client):
    return IncidentDetector(conn, client, gov=None)


def audit_kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM audit WHERE kind NOT IN ('intent', 'intent_dropped') ORDER BY id")]


@pytest.mark.parametrize("connected,halluc,fires", [
    (199, 20, False),    # not a population yet
    (2000, 50, False),   # 2.5%: significant, but not > 2x baseline
    (206, 8, False),     # 3.9% over 206 calls: the public_01 false alarm. Exact tail p ~ 0.007, just noise
    (600, 26, True),     # 4.3% over 600 calls: big and very unlikely to be noise
])
def test_detection_thresholds(conn, connected, halluc, fires):
    d = det(conn, FakeLLM({}))
    cur = {"connected": connected, "halluc": halluc, "engaged": 0, "dials": 0}
    assert d.is_incident(cur, d.baseline("wf", D0)) is fires


def test_choice_is_deterministic_and_prefers_known_good():
    pick = IncidentDetector.choose
    vals = lambda **kw: {p: {"pass_rate": r} for p, r in kw.items()}  # noqa: E731
    assert pick({"known_good": "P1", "validations": vals(P1=0.98, P3=0.995)}, 0.97) == "P1"
    assert pick({"known_good": "P1", "validations": vals(P1=0.96, P3=0.99, P4=0.995)}, 0.97) == "P4"
    assert pick({"known_good": "P1", "validations": vals(P1=0.9, P3=0.95)}, 0.97) is None


def test_full_flow_files_urgent_intent_with_validation_evidence(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    det(conn, llm).run(NOW)
    assert llm.suggest_calls == 1 and llm.validate_calls == ["P1", "P3", "P4"]   # bad prompt P2 never validated
    intent = conn.execute("SELECT * FROM intents").fetchone()
    assert intent["status"] == "open" and '"urgent": true' in intent["body_json"]
    assert '"validation_run_id": "val_P1"' in intent["body_json"]
    assert audit_kinds(conn) == ["incident_detected", "llm_suggested", "validated", "validated", "validated",
                                 "incident_propose"]


def test_restart_mid_flow_does_not_pay_twice(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    real = llm.validate
    llm.validate = lambda wid, pv, n: (_ for _ in ()).throw(SystemExit("kill -9")) if pv == "P3" else real(wid, pv, n)
    with pytest.raises(SystemExit):
        det(conn, llm).run(NOW)
    llm.validate = real
    d = det(conn, llm)
    state = store.kv_get(conn, "incident:wf")
    d._fix("wf", {"governance": GOV}, state, NOW)       # what the restarted flow resumes with
    assert llm.suggest_calls == 1 and llm.validate_calls == ["P1", "P3", "P4"]


def test_verify_resolves_when_rate_drops(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    d = det(conn, llm)
    d.run(NOW)
    llm.prompt = "P1"
    d.run("2027-01-08T11:00:00+05:30")                   # sees P1 live -> verifying
    add_calls(conn, "2027-01-08T11", "P1", 300, 4, 150)
    d.run("2027-01-08T14:00:00+05:30")
    assert audit_kinds(conn)[-2:] == ["incident_applied", "incident_resolved"]
    assert store.kv_get(conn, "incident:wf") is None


def test_verify_reverts_to_known_good_if_fix_fails(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.96, "P3": 0.99, "P4": 0.995})   # known-good fails validation here -> P4 chosen
    d = det(conn, llm)
    d.run(NOW)
    assert store.kv_get(conn, "incident:wf")["chosen"] == "P4"
    llm.prompt = "P4"
    d.run("2027-01-08T11:00:00+05:30")
    add_calls(conn, "2027-01-08T11", "P4", 300, 3, 60)      # safe, but engagement 20% vs 50%: guardrail
    d.run("2027-01-08T14:00:00+05:30")
    # P1 didn't pass validation, so there's no safe revert target: alert instead of guessing
    assert audit_kinds(conn)[-1] == "incident_unresolved"


def test_no_passing_candidate_alerts(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    det(conn, FakeLLM({"P1": 0.9, "P3": 0.9, "P4": 0.9})).run(NOW)
    assert audit_kinds(conn)[-1] == "incident_no_fix"
    assert conn.execute("SELECT COUNT(*) n FROM intents").fetchone()["n"] == 0


def test_out_of_credits_alerts(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    det(conn, FakeLLM({"P1": 0.99, "P3": 0.99, "P4": 0.99}, credits=8)).run(NOW)
    assert audit_kinds(conn)[-1] == "incident_no_credits"


def test_supervisor_change_supersedes_flow(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    d = det(conn, llm)
    d.run(NOW)
    llm.prompt = "P5"                                       # supervisor picked something else
    d.run("2027-01-08T09:00:00+05:30")
    assert audit_kinds(conn)[-1] == "incident_superseded"


def test_waiting_keeps_full_rationale_for_the_approver(conn):
    add_calls(conn, "2027-01-07T15", "P2", 600, 26, 300)
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    d = det(conn, llm)
    d.run(NOW)
    first = conn.execute("SELECT body_json FROM intents").fetchone()["body_json"]
    d.run("2027-01-07T20:00:00+05:30")      # still waiting for approver hours
    assert conn.execute("SELECT body_json FROM intents").fetchone()["body_json"] == first
    assert "passed validation" in first and "validation_pass_rates" in first


def test_kpi_check_follows_the_workflow_objective():
    base = {"dials": 10000, "connected": 5000, "engaged": 2500}
    pickup_down = {"dials": 4000, "connected": 1800, "engaged": 900}     # pickup 45% vs 50%, engagement 50% flat
    kyc = {"primary": "pickup_rate", "guardrail": {"engagement_rate_max_drop": 0.02}}
    assert IncidentDetector.kpis_fell(pickup_down, base, OBJECTIVE) == ["pickup_rate fell (guardrail: 45.0% vs 50.0%)"]
    assert IncidentDetector.kpis_fell(pickup_down, base, kyc) == []        # 10% relative: not past the primary limit
    eng_down = {"dials": 4000, "connected": 2000, "engaged": 920}        # engagement 46% vs 50%: 4 points, 8% relative
    assert IncidentDetector.kpis_fell(eng_down, base, OBJECTIVE) == []     # primary: under the 10% relative limit
    assert IncidentDetector.kpis_fell(eng_down, base, kyc) == ["engagement_rate fell (guardrail: 46.0% vs 50.0%)"]
    noise = {"dials": 400, "connected": 190, "engaged": 95}             # 47.5% pickup on 400 dials: not significant
    assert IncidentDetector.kpis_fell(noise, base, OBJECTIVE) == []


def test_verify_compares_with_the_healthy_day_before_the_bad_prompt_not_history(conn):
    # pickup fell on D2 for another reason (a flagged caller ID): 600 / 3600 = 16.7% vs 25% in history
    conn.execute("INSERT INTO stats VALUES ('wf', '2027-01-06T15', 1, 'default', 'prompt_version', 'P1', 3600, 600, 300, 8)")
    conn.execute("INSERT INTO stats VALUES ('wf', '2027-01-07T15', 2, 'default', 'prompt_version', 'P2', 3600, 600, 300, 26)")
    llm = FakeLLM({"P1": 0.985, "P3": 0.993, "P4": 0.973})
    d = det(conn, llm)
    d.run(NOW)
    llm.prompt = "P1"
    d.run("2027-01-08T11:00:00+05:30")
    conn.execute("INSERT INTO stats VALUES ('wf', '2027-01-08T11', 3, 'default', 'prompt_version', 'P1', 1800, 300, 150, 4)")
    d.run("2027-01-08T14:00:00+05:30")
    assert audit_kinds(conn)[-1] == "incident_resolved"     # vs history (25%) this was a false guardrail alert
