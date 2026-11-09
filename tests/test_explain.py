"""explain answers 'why did X change at T, who approved, what next' from the store alone."""
import json

from controller import explain, store

D0 = "2027-01-04T00:00:00+05:30"


def build(tmp_path):
    path = tmp_path / "s.db"
    c = store.open_db(path)
    store.kv_put(c, "world", {"scenario": "t", "d0": D0})
    c.execute("INSERT INTO stats VALUES ('wf','2027-01-07T12',2,'default','prompt_version','P2',100,20,8,4)")
    c.execute("INSERT INTO stats VALUES ('wf','2027-01-08T12',3,'default','prompt_version','P1',100,20,10,0)")
    store.add_audit(c, "2027-01-07T11:30:00+05:30", "supervisor_edit", "supervisor changed prompt_version -> v2",
                    "supervisor", "wf", evidence={"version": 2, "changed_dims": ["prompt_version"]})
    payload = {"change_summary": "switch prompt P2 -> P1", "rationale": "hallucination spike", "diff": {"x": 1},
               "impact_score": 1.0, "affected_leads": 10, "evidence": {"validation_run_id": "val_1"}, "dry_run": {"a": 1}}
    store.insert_proposal(c, "p1", "wf", 2, payload, "2027-01-08T10:00:00+05:30")
    store.update_proposal(c, "p1", approval_id="apr_1", status="applied", applied_at="2027-01-08T11:00:00+05:30")
    store.add_audit(c, "2027-01-08T10:42:00+05:30", "approval_decided", "approver: approved", "approver", "wf",
                    approval_id="apr_1")
    store.add_audit(c, "2027-01-08T11:00:00+05:30", "config_applied", "controller_approved changed prompt_version -> v3",
                    "controller_approved", "wf", proposal_id="p1", approval_id="apr_1",
                    evidence={"version": 3, "changed_dims": ["prompt_version"]})
    c.close()
    return explain.connect(path)


def test_controller_change_explains_why_who_and_after(tmp_path):
    out = explain.explain_at(build(tmp_path), "wf", "D4 12:00")
    assert "Live config: v3 since D4 11:00" in out
    assert "switch prompt P2 -> P1" in out and "val_1" in out
    assert "D4 10:42 approver (apr_1): approver: approved" in out
    assert "before" in out and "after" in out


def test_supervisor_change_is_attributed_to_supervisor(tmp_path):
    out = explain.explain_at(build(tmp_path), "wf", "D3 15:00")
    assert "by supervisor" in out and "outside governance" in out


def test_before_any_change_is_business_default(tmp_path):
    assert "business default" in explain.explain_at(build(tmp_path), "wf", "D1 09:00")


def test_view_lists_each_workflow(tmp_path):
    out = explain.view(build(tmp_path))
    assert "## wf" in out and "Waiting on you: nothing" in out


def test_parse_at_roundtrip(tmp_path):
    c = build(tmp_path)
    assert explain.parse_at(c, "D3 14:00") == "2027-01-07T14:00:00+05:30"
    assert explain.label(c, "2027-01-07T14:00:00+05:30") == "D3 14:00"
    assert json.dumps(explain.kpis(c, "wf", "2027-01-07T00:00:00+05:30", "2027-01-08T00:00:00+05:30"))
