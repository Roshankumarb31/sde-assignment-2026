"""Hallucination incident response (C8): detect -> ask LLM once -> validate -> propose with evidence ->
verify after apply -> revert if the fix didn't work.

Detection (per workflow, on the prompt that is live right now):
  recent = connected calls on the current prompt over the last 2 h, widened step by step up to 24 h until
           there are MIN_CONNECTED of them. Only calls on the CURRENT prompt count, so a wider window never
           mixes in the old prompt; it just lets low-volume workflows reach a population sooner.
  base   = hallucination rate in history (before D0), the business-default world (thousands of calls,
           so we treat it as known)
  incident if, for some window, connected >= 200 AND rate > 2 x base AND the exact binomial tail
  P(X >= hallucinations | n, base) < 0.001 / (24 x number of windows).

  Why exact binomial: ~200 calls at a ~1.4% base rate means ~3 expected hallucinations; a z-test is badly
  anti-conservative there (it fired a false incident on public_01).
  Why divide 0.001: we look every hour at several overlapping windows. Spending 0.001 across one day's
  looks (Bonferroni) keeps the false-alarm rate near 1 per workflow per 1000 days, instead of near-certain
  over a scenario.

Credits (only spent after an incident is detected, at most one flow per workflow at a time):
  suggest-fixes once (3) + validation n=400 (4 each) for each suggestion and for the last known-good prompt.
  Each result is saved before the next call, so a restart doesn't pay twice.

Choosing the fix is deterministic, never the LLM:
  only prompts with pass_rate >= min_validation_pass_rate qualify; the known-good prompt wins if it
  qualifies (it has real engagement history; validation says nothing about engagement), else the highest
  pass_rate. The change goes through the approver with evidence.validation_run_id, marked urgent.

Verification after apply: once the new prompt has MIN_CONNECTED connected calls, if the incident test
still fires, or engagement fell significantly versus history, we revert to the known-good prompt.
"""
import logging
from datetime import datetime, timedelta

from controller import governance as g
from controller import store
from controller.client import CreditsExhausted
from controller.stats import binom_sf, p_less, rate

log = logging.getLogger("incident")

MIN_CONNECTED = 200
RATE_MULTIPLE = 2.0
P_INCIDENT = 0.001
P_ENGAGEMENT_DROP = 0.01
ENGAGEMENT_MAX_REL_DROP = 0.10
VALIDATION_N = 400
WINDOW_HOURS = (2, 6, 24)
P_PER_LOOK = P_INCIDENT / (24 * len(WINDOW_HOURS))


def _hour(ts, minus_hours=0):
    return (datetime.fromisoformat(ts) - timedelta(hours=minus_hours)).isoformat()[:13]


class IncidentDetector:
    def __init__(self, conn, client, gov):
        self.conn, self.client, self.gov = conn, client, gov

    # ---------------------------------------------------------------- loop entry

    def run(self, now):
        d0 = store.kv_get(self.conn, "world", {}).get("d0")
        if not d0:
            return
        for wf in self.client.workflows():
            wid = wf["workflow_id"]
            state = store.kv_get(self.conn, f"incident:{wid}")
            prompt = self.client.config(wid)["config"]["prompt_version"]
            if state is None:
                self._detect(wid, wf, prompt, d0, now)
            elif state["phase"] == "proposed":
                self._wait_for_apply(wid, state, prompt, now)
            elif state["phase"] == "verifying":
                self._verify(wid, wf, state, prompt, d0, now)

    # ---------------------------------------------------------------- measurements

    def baseline(self, wid, d0):
        t = store.total_stats(self.conn, wid, "0000", until_hour=d0[:13])
        return t

    def window(self, wid, prompt, since_hour):
        empty = {"connected": 0, "halluc": 0, "engaged": 0, "dials": 0}
        return store.sum_stats(self.conn, wid, "prompt_version", since_hour).get(prompt) or empty

    def worst_window(self, wid, prompt, base, now):
        """Of the 2 h / 6 h / 24 h windows on the current prompt, the one with the smallest tail p-value.
        Short windows catch a fresh spike on a busy workflow; long ones give small workflows a population.
        Only calls on the current prompt count, so a long window never mixes in an older prompt."""
        wins = [self.window(wid, prompt, _hour(now, h)) for h in WINDOW_HOURS]
        return min(wins, key=lambda w: self.p_value(w, base))

    @staticmethod
    def base_rate(base):
        return (base["halluc"] + 1) / (base["connected"] + 2)   # +1/+2 so a clean history isn't a zero rate

    def p_value(self, cur, base):
        return binom_sf(cur["halluc"], cur["connected"], self.base_rate(base))

    def is_incident(self, cur, base):
        return (cur["connected"] >= MIN_CONNECTED
                and rate(cur["halluc"], cur["connected"]) > RATE_MULTIPLE * self.base_rate(base)
                and self.p_value(cur, base) < P_PER_LOOK)

    # ---------------------------------------------------------------- phase: detect + fix

    def _detect(self, wid, wf, prompt, d0, now):
        base = self.baseline(wid, d0)
        cur = self.worst_window(wid, prompt, base, now)
        if not self.is_incident(cur, base):
            return
        evidence = {"prompt": prompt, "recent": cur, "baseline": base,
                    "recent_rate": round(rate(cur["halluc"], cur["connected"]), 4),
                    "baseline_rate": round(rate(base["halluc"], base["connected"]), 4),
                    "p_value": self.p_value(cur, base), "p_threshold": P_PER_LOOK}
        known_good = self._known_good_prompt(wid, prompt, d0)
        state = {"phase": "fixing", "bad_prompt": prompt, "known_good": known_good, "detected_at": now,
                 "detect_evidence": evidence, "suggestions": None, "validations": {}}
        self._save(wid, state, now, "incident_detected",
                   f"hallucination rate {evidence['recent_rate']:.1%} on {prompt} vs {evidence['baseline_rate']:.1%} "
                   f"baseline ({cur['connected']} connected calls, p={evidence['p_value']:.1e})", evidence)
        self._fix(wid, wf, state, now)

    def _fix(self, wid, wf, state, now):
        min_pass = wf["governance"]["min_validation_pass_rate"]
        try:
            if state["suggestions"] is None:
                ev = state["detect_evidence"]
                res = self.client.suggest_fixes(wid, f"hallucination rate {ev['recent_rate']:.1%} on prompt "
                                                     f"{state['bad_prompt']} vs {ev['baseline_rate']:.1%} baseline")
                state["suggestions"] = [s for s in res["suggestions"] if s["prompt_version"] != state["bad_prompt"]]
                self._save(wid, state, now, "llm_suggested",
                           "LLM suggested " + ", ".join(f"{s['prompt_version']} ({s['summary']})"
                                                        for s in state["suggestions"]),
                           {"suggestions": res["suggestions"], "credits_remaining": res.get("credits_remaining")})
            candidates = ([state["known_good"]] if state["known_good"] else []) + \
                [s["prompt_version"] for s in state["suggestions"] if s["prompt_version"] != state["known_good"]]
            for pv in candidates:
                if pv in state["validations"]:
                    continue
                v = self.client.validate(wid, pv, VALIDATION_N)
                state["validations"][pv] = {"run_id": v["validation_run_id"], "pass_rate": v["pass_rate"], "n": v["n"]}
                self._save(wid, state, now, "validated", f"validation {pv}: pass_rate {v['pass_rate']:.3f} (n={v['n']})",
                           {"validation": v})
        except CreditsExhausted as e:
            self._close(wid, now, "incident_no_credits", f"ALERT: out of credits mid-incident ({e.detail}); "
                        "page on-call to fix the prompt by hand", state)
            return
        choice = self.choose(state, min_pass)
        if choice is None:
            self._close(wid, now, "incident_no_fix", "ALERT: no candidate prompt passed validation "
                        f"(need >= {min_pass}); page on-call", state)
            return
        self._propose(wid, state, choice, now, reason="fix")

    @staticmethod
    def choose(state, min_pass):
        """Deterministic winner: known-good prompt if it passes, else the highest pass_rate that passes."""
        passing = {pv: v for pv, v in state["validations"].items() if v["pass_rate"] >= min_pass}
        if not passing:
            return None
        if state["known_good"] in passing:
            return state["known_good"]
        return max(sorted(passing), key=lambda pv: passing[pv]["pass_rate"])

    def _propose(self, wid, state, choice, now, reason):
        v = state["validations"][choice]
        ev = state["detect_evidence"]
        summary = f"switch prompt {state['bad_prompt']} -> {choice} to stop a hallucination spike"
        rationale = (f"Hallucinations on {state['bad_prompt']}: {ev['recent_rate']:.1%} vs {ev['baseline_rate']:.1%} "
                     f"baseline over {ev['recent']['connected']} connected calls (p={ev['p_value']:.1e}). "
                     f"{choice} passed validation at {v['pass_rate']:.1%} (n={v['n']}, run {v['run_id']}).")
        if choice == state["known_good"]:
            rationale += f" {choice} is the last known-good prompt, so its engagement is known from history."
        dry_run = {"bad_prompt": {"prompt": state["bad_prompt"], **ev["recent"]},
                   "baseline_history": ev["baseline"],
                   "validation_pass_rates": {pv: x["pass_rate"] for pv, x in state["validations"].items()},
                   "chosen": choice}
        state.update(phase="proposed", chosen=choice, proposed_at=now, attempt=state.get("attempt", 0) + 1)
        self._save(wid, state, now, "incident_propose", f"{reason}: {summary}", dry_run)
        g.add_intent(self.conn, now, wid, "incident_prompt", [[["prompt_version"], choice]], summary, rationale,
                     severity=1.0, dry_run=dry_run, evidence={"validation_run_id": v["run_id"]}, urgent=True)

    # ---------------------------------------------------------------- phase: wait for apply

    def _wait_for_apply(self, wid, state, prompt, now):
        if prompt == state["chosen"]:
            state.update(phase="verifying", applied_at=now)
            self._save(wid, state, now, "incident_applied", f"prompt {state['chosen']} is live; verifying", {})
        elif prompt != state["bad_prompt"]:
            self._close(wid, now, "incident_superseded",
                        f"prompt changed to {prompt} by someone else (supervisor); stopping this incident flow", state)
        else:
            # keep the request alive; this is a no-op while it's in flight and re-files after a rejection cool-down
            v = state["validations"][state["chosen"]]
            g.add_intent(self.conn, now, wid, "incident_prompt", [[["prompt_version"], state["chosen"]]],
                         f"switch prompt {state['bad_prompt']} -> {state['chosen']} to stop a hallucination spike",
                         "hallucination incident still open", 1.0, {"see": "incident_propose audit row"},
                         evidence={"validation_run_id": v["run_id"]}, urgent=True)

    # ---------------------------------------------------------------- phase: verify

    def _verify(self, wid, wf, state, prompt, d0, now):
        if prompt != state["chosen"]:
            return self._close(wid, now, "incident_superseded",
                               f"prompt changed to {prompt} by someone else during verification", state)
        after = self.window(wid, prompt, state["applied_at"][:13])
        if after["connected"] < MIN_CONNECTED:
            return
        base = self.baseline(wid, d0)
        evidence = {"after": after, "baseline": base, "after_rate": round(rate(after["halluc"], after["connected"]), 4),
                    "after_engagement": round(rate(after["engaged"], after["connected"]), 4),
                    "baseline_engagement": round(rate(base["engaged"], base["connected"]), 4)}
        still_bad = self.is_incident(after, base)
        eng_drop = (rate(after["engaged"], after["connected"]) < (1 - ENGAGEMENT_MAX_REL_DROP) * rate(base["engaged"], base["connected"])
                    and p_less(after["engaged"], after["connected"], base["engaged"], base["connected"]) < P_ENGAGEMENT_DROP)
        if not still_bad and not eng_drop:
            return self._close(wid, now, "incident_resolved",
                               f"fixed: hallucination {evidence['after_rate']:.1%} on {prompt} "
                               f"(was {state['detect_evidence']['recent_rate']:.1%}), engagement "
                               f"{evidence['after_engagement']:.1%} vs {evidence['baseline_engagement']:.1%} history",
                               state, evidence)
        why = "hallucinations still high" if still_bad else "engagement fell (guardrail)"
        good = state["known_good"]
        if good and prompt != good and good in state["validations"] and \
                state["validations"][good]["pass_rate"] >= wf["governance"]["min_validation_pass_rate"]:
            self._save(wid, state, now, "incident_revert", f"{why} on {prompt}; reverting to known-good {good}", evidence)
            state["bad_prompt"] = prompt
            return self._propose(wid, state, good, now, reason="revert")
        self._close(wid, now, "incident_unresolved", f"ALERT: {why} on {prompt} and no safer prompt left; page on-call",
                    state, evidence)

    # ---------------------------------------------------------------- helpers

    def _known_good_prompt(self, wid, bad_prompt, d0):
        """The prompt the business ran in history (most connected calls before D0), unless it's the bad one."""
        hist = store.sum_stats(self.conn, wid, "prompt_version", "0000", until_hour=d0[:13])
        ranked = sorted(((s["connected"], pv) for pv, s in hist.items() if pv != bad_prompt), reverse=True)
        return ranked[0][1] if ranked else None

    def _save(self, wid, state, now, kind, summary, evidence):
        with store.atomic(self.conn):
            store.kv_put(self.conn, f"incident:{wid}", state)
            store.add_audit(self.conn, now, kind, summary, "controller", wid, evidence=evidence)
        log.info("%s %s: %s", wid, kind, summary)

    def _close(self, wid, now, kind, summary, state, evidence=None):
        with store.atomic(self.conn):
            self.conn.execute("DELETE FROM kv WHERE key = ?", (f"incident:{wid}",))
            history = store.kv_get(self.conn, f"incident_history:{wid}", [])
            store.kv_put(self.conn, f"incident_history:{wid}", history + [{**state, "closed": kind, "closed_at": now}])
            store.add_audit(self.conn, now, kind, summary, "controller", wid, evidence=evidence or {})
        g.withdraw_intent(self.conn, now, wid, "incident_prompt", kind)
        log.warning("%s %s: %s", wid, kind, summary)
