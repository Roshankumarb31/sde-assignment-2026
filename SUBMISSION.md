# Outcome-First Agent Loop — Submission

**Name:** Roshan Kumar B · **Date:** 2026-11-09 · **Hours actually spent (be honest):** ⟨HOURS⟩ ·
**Language / storage:** Python 3.12, `httpx` (already in requirements.txt), SQLite from the standard library (one
file, WAL mode). No new services, no new dependencies.

---

## 0. TL;DR

- A single-process controller (`./run.sh <SIM_URL>`) that ingests every event exactly once into SQLite, detects
  hallucination incidents with an exact binomial test, fixes them through the approver with validation evidence,
  verifies the fix against the workflow's own primary KPI and guardrail, and can explain every change from its store.
- **public_02:** the supervisor's bad prompt on ks_cancel was detected after 7.5 h, fixed with the known-good prompt via
  one approval, and verified. Hallucinated calls on ks_cancel fell **330 → 77**; the change is graded **beneficial**;
  MTTR 1410 min; 15 of 200 credits. **public_01:** zero changes, zero false alarms, zero credits.
- kill -9 six times mid-scenario (including with an approval pending) → grade **identical** to the uninterrupted run.
- public_03 (12 workflows, ~850k events/day): one simulated day ingested **and decided** in **20.6 s** wall (target < 600 s), 33 MB peak memory, ingest lag 0.
- **Deliberately not built:** experiments (C6/C7), call-time optimisation (C4), caller-ID health (C5). So outside
  incidents the controller makes no changes, and I get no lift from experiments. I chose safety and governance first.

## 1. Assumptions

1. **D0** is `ts − sim_minute` from `GET /sim/state`. Everything before it is *history*: the business-default
   world, treated as healthy and used as the baseline for hallucination rates.
2. **History is large enough to be treated as known** (16k+ connected calls per workflow on public_02), so the
   incident test compares a recent window against a fixed rate, not two samples. A smoothed rate (x+1)/(n+2) is used
   so a workflow with a perfectly clean history doesn't get a zero baseline.
3. **The "known-good" prompt** is the prompt with the most connected calls in history, excluding the bad one.
   Validation measures hallucination only, not engagement; the known-good prompt is the only candidate whose
   engagement we have actually observed.
4. **`*_max_drop` in the objective is an absolute drop** in rate (0.02 = 2 percentage points). This is how the grader
   labels changes (`dd < -0.02`), and the safer reading.
5. **Approvers answer in roughly the middle of `approver_latency_minutes`.** A normal request is only sent if that
   average latency still fits before the end of the approver's day.
6. **Customer-facing dimensions** are welcome_message, voice, prompt_version and cohorts. They always go to a human,
   even where `auto_apply.allowed_dims` would allow them (Northwind compliance memo; Kestrel supervisor memo).
   Operational dimensions (caller_id, schedule) may be auto-applied within policy.
7. **A request sent by the controller counts against `max_requests_per_24h` until it's decided**, including
   rejected and expired ones. Only rows the simulator never received (`abandoned`) don't count.
8. **The simulator may restart independently**; if its scenario differs or its event counter is behind our cursor, our
   store belongs to another world. We move it aside rather than mix two worlds.
9. **Sim time, not wall time, drives every decision.** That's what makes the crash test comparable: the same inputs
   at the same sim times produce the same decisions.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion at load scale | **Built** | Everything else is only as good as its data; the hard rule on crash safety starts here. | — |
| C2 | Shared cohort object | Partial | Stats are keyed by the config's `cohort_id`; governance counts affected leads with the same cohort definitions. Detection is per workflow, not per cohort. | Per-cohort detection windows; cohort registry with stable IDs across config versions. |
| C3 | Bootstrapping from history | Partial | History is the incident baseline and picks the known-good prompt. Not used to warm-start experiments (there are none). | Priors per arm/cohort from history for C6. |
| C4 | Call-time optimisation | **Cut** | Per-person needs many attempts per person; cadence gives 2–8. A bad version shifts capacity to wrong hours for everyone. | Per-cohort hour weights from history, explored with a small budget; per-person only for returning people with enough history. ~1 day. |
| C5 | Caller-ID health | **Cut** (next on my list) | public_02 flags a DID on D2 and I leave it in rotation. Chose to finish incidents + governance + crash safety properly first. | Per-DID pickup vs other DIDs on the same workflow (two-proportion test, Bonferroni over DIDs); auto-apply a weight shift within `max_shift`; restore slowly. ~2–3 h on top of existing intents/governance. |
| C6 | Welcome × voice experiments | **Cut** | The biggest piece. Doing it badly (false winners, fragmented traffic) is worse than not doing it. | See §5 for the design I'd build. ~1.5 days. |
| C7 | Promotion, decay, rollback | **Cut** | Depends on C6. The verify-and-revert part exists for incident fixes. | Reuse the incident verifier per winner, rolling window. |
| C8 | Hallucination incident response | **Built** | Graded directly (MTTR, harmful changes); uses credits, so it must be careful. | — |
| C9 | Governance client | **Built** | The governance boundary is absolute; one mistake fails the submission. | — |
| C10 | Impact, affected leads, dry run | Partial | Every proposal has all four (§6) and they're honest, but the dry run is an evidence summary, not a counterfactual replay. | Replay history through the proposed config per cohort. |
| C11 | Supervisor view | Partial | `python -m controller.explain --view`: one screen per workflow, from the store. CLI, not UI. | Small read-only HTTP endpoint over the same function. |
| C12 | Audit store, "why did X change" | **Built** | Mandatory floor; it's also how the review is run. | — |
| C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable and a hard rule. | — |
| C14 | Loop observability | Partial | One log line per sim hour (lag, decisions, open proposals, credits) and a `health` record the view shows. No metrics endpoint. | Prometheus counters + the alert list in §10. |
| C15 | Fair credit/attention allocation | Cut | Today: credits only after a confirmed incident, one flow per workflow at a time, ≤ 15 credits per incident; requests capped per workflow. | A per-tenant credit budget and a global approver-attention queue ranked by impact. |

**Principle.** I ranked by three questions, in order. (1) *What can fail the whole submission?* The governance
boundary, the audit trail, crash safety and credit discipline — so C9, C12, C13 and C1 came first and are tested
against the live simulator, including kill -9. (2) *What do the scenarios grade directly?* Incident time-to-fix
and harmful changes — C8. (3) *Everything else* is valuable but has a bad failure mode when rushed (false winners,
traffic shifted to the wrong hours), so I cut it and wrote down how I'd build it, rather than ship a sketch.

## 3. Stakeholders

Every claim below was checked against `GET /workflows/{id}` on all three public scenarios, not against the memos.

### 01 · Nikhil Rao, VP Sales

- **Accepted:** speed where it's safe. Incident fixes are urgent: they use the reserved request, skip the quiet
  hour and get a 2 h (not 24 h) cool-down after a rejection. Operational changes may auto-apply within policy.
- **Declined:** a single blended score (Product's definition and a hard constraint: pickup and engagement are
  reported separately everywhere, including `explain`). "Winning by lunch, live for everyone by evening": a winner
  needs a minimum population and a significance test, and `max_shift` limits how much traffic one auto-applied change
  can move (0.2 on Kestrel cancel). Rolling out the "persuasive" prompt: no prompt goes live without validation and a
  named approval, and promoting a variant to other clients is out of scope (Product memo).
- **Wrong claims:**
  - *"Kestrel has turned on auto-apply for everything"* — false. ks_cancel's `allowed_dims` are
    schedule, caller_id, welcome_message and voice: **not prompt_version**, and `max_shift` is 0.2. In public_03,
    Kestrel's KYC workflow (kest_kyc_1) has auto-apply **disabled** entirely.
  - *"Northwind is fine with auto-apply on KYC"* — false. nw_kyc has `auto_apply.enabled: false`,
    `allowed_dims: []`, and Northwind's own compliance head requires a named human approval for anything a
    customer hears. KYC is also `mid_flight: next_campaign_only`, so nothing there goes live "by evening" anyway.
  - *"The persuasive prompt is available"* — P4 isn't in the KYC prompt catalogs at all, and on ks_cancel it validated
    at 97.25%, just above the 97% bar and the weakest of the three candidates.
- **Reply:**
  > Nikhil, happy to make the summit story real, with numbers we can defend. Two corrections first: Kestrel's
  > auto-apply excludes prompts and caps each change at 20% of traffic, and Northwind KYC has auto-apply off. Their
  > compliance head wants a named approval for anything a customer hears. So the story is "the agent caught a bad
  > prompt overnight, proved the fix, and the client's supervisor approved it in 42 minutes." That's on public_02
  > with the audit trail behind it. I'll give one number per KPI per client, never a blended score (Product's rule).
  > The persuasive prompt only ships per client, after validation and approval.

### 02 · Meera Iyer, Head of Compliance, Northwind Life

- **Accepted, and where it shows up:**
  1. No customer-facing change without a named approval: `CUSTOMER_FACING` in `governance.py` forces the approval route
     for welcome_message, voice, prompt_version and cohorts, *even where the policy would let us auto-apply*.
     Operational latitude (caller_id, schedule on nw_renewal) is used only within `allowed_dims`/`max_shift`.
  2. Script changes carry validation evidence: every prompt proposal has `evidence.validation_run_id`, the pass rates
     of all candidates in the dry run, and the rationale states the numbers. The LLM only suggests; a fixed rule picks.
  3. Auditors' "why did this customer hear this": `controller.explain --workflow X --at T` answers which config was
     live, why, who approved and the evidence, in seconds, from our store.
  4. "Slow and right": incident threshold p < 0.001 per day with Bonferroni over looks; after a fix, the primary KPI
     and the guardrail are checked and the change is reverted if either fell significantly.
  5. Approver hours come from the policy (10–19, KYC 10–18). Nothing is sent outside them; a normal request is only
     sent if the approver can answer it the same day.
- **Pushed back / limits:** the 24 h audit answer is per workflow, config version and cohort, not per individual
  customer: we store aggregates, not a per-call log. The weekly summary of operational changes isn't built; today
  they're in the audit store and the supervisor view.
- **Wrong claims:** none. Her approver hours match the policy exactly.
- **Reply:**
  > Meera, thank you, this matches how the controller works. Anything a customer hears (opening line, voice, script)
  > always goes to your named approver, even where the signed policy would allow auto-apply. Script changes carry a
  > validation run ID and the pass rates of every candidate. The model suggests options but never chooses. For your
  > auditors, we can answer "what was live on this workflow at this time, why, and who approved it" in minutes. Per
  > customer, today we answer at the cohort and version level; per-call lookup is on the roadmap. Requests only go
  > out in your approvers' hours.

### 03 · Kabir Menon, Platform & SRE

- **Accepted:**
  - *Survives kill at any moment*: `scripts/crash_test.sh`, 6 × kill -9 on public_02 including with an approval pending
    and right after the approver said yes. Grade identical to the uninterrupted run (§9).
  - *Idempotency*: chunk-level replay skip, write-ahead proposal rows, deterministic `proposal_id`, reconcile against
    `GET /approvals` and config history on restart (§9).
  - *Loop health without logs*: `explain --view` shows ingest lag and credits from a `health` record written each hour.
  - *One day of public_03 in < 10 min*: met with a wide margin: a full simulated day in 20.6 s wall, 33 MB peak (§8).
- **Pushed back:** SQLite *is* state, even if it isn't a service. It's an embedded file: no daemon, nothing to page on
  at 2 a.m., backup is a file copy. But if nodes are recycled the file must live on a persistent volume, and at
  production scale I'd move the same schema to your Postgres (§14). No Redis needed.
- **Alerts:** only actionable ones page (§10). An incident that the controller is handling doesn't page anyone.
- **Wrong claims:** none.
- **Reply:**
  > Kabir, the kill test is in the repo: `scripts/crash_test.sh` kills the controller six times mid-run, including with
  > an approval in flight. The final grade is identical, with no duplicate apply. State is one SQLite file (no
  > daemon); it needs a persistent volume, and I'd move it to our Postgres before production. public_03:
  > one simulated day ingested and decided in about 21 seconds (target: 10 minutes), 33 MB of memory. At 10× the single-writer process and the hourly detector pass are the limits; the fix is
  > sharding by tenant. It pages only when the controller can't act: a fix that failed with nothing left to try,
  > credits gone mid-incident, or ingest falling behind.

### 04 · Anita Desai, Operations Supervisor, Kestrel Finance

- **Accepted, and where it shows up:**
  - *Don't undo my changes*: a supervisor edit is ingested as an audit row (actor `supervisor`). An open intent on the
    same dimension is dropped, and every change is built on the latest config version. A 409 stale_base_version
    re-proposes on her version instead of overwriting it.
  - *Two requests a day, none before 11:00, batch*: at most **2 normal requests per workflow per 24 h**, even though
    ks_cancel's policy allows 4 (`MAX_NORMAL_REQUESTS_PER_24H`). The first approver hour is skipped for normal
    requests, and all of a workflow's pending changes go out as one request ("N changes: …").
  - *Tell me before anything changes*: every customer-facing change on her line is an approval request.
  - *One screen*: `explain --view` shows, per workflow, what's live, open incidents, what's being tested, what we
    want from her, what changed in the last 24 h, and alerts.
  - *The word-for-word script*: the fix prefers the known-good prompt, whose engagement is known. After any fix,
    engagement (ks_cancel's primary KPI) is verified and the fix is reverted if it fell.
- **Pushed back:** an **incident fix** is the one exception. It may use a third (reserved) request, and it may arrive at
  10:00 rather than 11:00: on public_02 the hallucination fix was sent at 10:00 and approved at 10:42.
  Waiting one more hour means one more hour of customers hearing invented claims. The fix also replaced *her* prompt
  edit (P2); it went to her for approval, with the evidence, rather than being reverted behind her back.
- **Wrong claims:** none; her limits are stricter than the policy (cap 4), so I follow hers.
- **Reply:**
  > Anita, you'll get at most two requests a day from us, never before 11:00, with everything batched into one. The
  > only exception is a hallucination fix. On D3 your prompt edit started making payment-urgency claims (4.2% of
  > calls vs 1.4% normally), and we asked you at 10:00 the next morning to switch back to P1, with the validation
  > results. Your changes are never undone silently; anything we want to change goes to you first. Your one screen
  > is `explain --view`. We prefer the old production prompt as a fix because we know people stay on the call with it.

### 05 · Tara Kulkarni, Head of Product

- **Accepted:**
  - *Separate KPIs*: never blended. Verification reads `objective.primary` and `objective.guardrail` from the policy:
    on cancel/renewal engagement is primary and pickup may not drop by more than 2 points; on KYC it's the reverse.
  - *One cohort object*: cohorts are the config's own cohort definitions; stats, affected-lead counts and the view all
    use that `cohort_id`.
  - *Proposals as a product surface*: every proposal has a summary, a diff, affected leads, a dry run, an impact score
    and a rationale (§6).
  - *Expired = rejection, re-queue with fresh data*: on expiry the intents re-open and a **new** proposal is built on
    the latest config and data (new proposal_id). The stale one is never resubmitted.
  - *Only what happened counts*: stats are keyed by the config version that was actually live for each call; an arm
    that never ran has no rows.
  - *Winners change their own workflow only*: true; nothing is promoted across workflows.
- **Pushed back:** her three P0s. Per-person call time: cut (§2, C4) — too few attempts per person to learn from, and a
  bad version hurts pickup for everyone. Supervisor view: built as a CLI screen. Caller-ID health: cut, and it's next.
  *Minimum cohort size*: I chose 200 connected calls for incident detection and verification (§5, §7) and wrote down
  the cost.
- **Wrong claims:** none; but "everything is P0" can't be honoured.
- **Reply:**
  > Tara, your definitions are in the code: KPIs never blended, and each workflow's primary and guardrail read from its
  > policy. Expired proposals are rebuilt on fresh data, and stats only count what actually ran. I couldn't do all three
  > P0s well, so I chose. Caller-ID health is next (about half a day on top of what exists). The supervisor view exists
  > as one screen. Per-person call time is cut: with 2–8 attempts per person, a per-person model learns noise and moves
  > capacity to the wrong hours. I'd start per cohort. Minimum population: 200 connected calls; costs and reasoning in
  > the doc.

### 06 · Aryamann, CTO

- **Accepted:** all of it as the reading guide. Explainability over size (every change carries evidence and can be
  replayed with `explain`); "best compute is no compute" (credits only after a statistically confirmed incident, 15 per
  incident, zero on a clean scenario); choosing (§2); docs for the on-call engineer (this file, module docstrings,
  `--view`).
- **Pushed back:** I can't claim it runs hundreds of workflows yet. It's single-process; §8 says what breaks first and
  how I'd shard.
- **Wrong claims (in the other memos)**: Sales on Kestrel and Northwind auto-apply (above).
- **Reply:**
  > Aryamann, I chose five capabilities and did them properly: exactly-once ingestion, governance, hallucination
  > incidents, an audit store that explains every change, and crash safety shown with kill -9. I said no to experiments,
  > per-person call time and caller-ID health for now, and wrote down why and what each would take. Two memo claims
  > were wrong against the policies (Kestrel and Northwind auto-apply); the controller follows the policy. Credits are
  > spent only after a confirmed incident. Scale numbers and the first thing that breaks at 10× are in §8.

## 4. Architecture

```
                           simulator (HTTP only)
   GET /events/chunks ─┐                       ┌─> POST /approvals, POST /workflows/{id}/apply
   GET /history/chunks ┤                       ├─> PUT /workflows/{id}/config   (auto route only)
                       │                       ├─> POST /llm/suggest-fixes, POST /validation/runs (incidents only)
                       v                       │
  ┌────────────────────────────────────────────┴────────────────────────────────────────────┐
  │ main.py  loop:  advance(60 min, 15 while an approval is open) → ingest → detectors → governance │
  │                                                                                          │
  │  ingest.py ──one transaction per chunk──> store (SQLite, WAL)                            │
  │     calls → hourly stats (workflow, hour, config_version, cohort, dim, value)            │
  │     leads → lead counts per cohort features      supervisor edits / decisions → audit    │
  │                                                                                          │
  │  incident.py (detector)  reads stats ──files an intent──> intents table                 │
  │                                                                                          │
  │  governance.py  intents ─batch per workflow─> ONE proposal ─write-ahead row─> HTTP       │
  │                                                                                          │
  │  explain.py  read-only over the same store:  --workflow X --at T,  --view                │
  └──────────────────────────────────────────────────────────────────────────────────────────┘
```

**State, and where it lives** (all in one SQLite file; nothing important lives only in memory between ticks):

| Table / key | What |
|---|---|
| `cursor` | last chunk ingested and last event id. Moves in the same transaction as the data. |
| `stats` | hourly counters (dials, connected, engaged, hallucinated) per config version, cohort and option. |
| `lead_counts`, `campaigns` | for affected-lead counts. |
| `intents` | what detectors want changed (open / proposed / done / dropped). |
| `proposals` | every request we've made, with status, base version, full payload, approval id. |
| `audit` | every decision, edit, approval and apply, with actor and evidence. |
| `kv` | incident flow state per workflow, cool-downs, world identity (scenario, D0), loop health. |

**On restart:** `run.sh` opens the same file. Ingest continues from the cursor (a chunk that was half-processed is
re-read whole; it was never committed). Governance first reconciles every open proposal against the simulator
(`GET /approvals`, `GET /workflows/{id}/config/history`). The incident flow resumes from its saved phase. Nothing is
inferred from wall time.

## 5. The learning loop

Most of this section is **design, not code**: I cut C4/C6/C7 (§2). What exists today is marked *built*.

- **Cohorts** *(partial)*: a cohort is the config's own cohort definition (`where` on lang, city tier, age band, value
  band). Stats are recorded per cohort_id; affected-lead counts use the same definitions. To avoid fragmenting traffic
  I'd only create a cohort when history shows a different response *and* the cohort gets ≥ 200 connected calls per arm
  within 3 days. Otherwise it stays in `default`.
- **Experiment design** *(not built)*: two arms at a time per dimension (current vs one challenger), 50/50 inside the
  cohort, welcome and voice tested one after the other, not crossed, so an interaction can't fake a winner and each
  test needs half the traffic of a 2×2. Challengers come from history (C3), not the LLM.
- **Deciding a winner** *(not built; the test exists)*: two-proportion test on the workflow's primary KPI, one-sided,
  α = 0.01 divided by the number of looks (one look per day); minimum 400 connected calls per arm; stop at a winner, at
  a loser, or after 5 days with no decision (the current arm stays). Expected false-winner rate ≤ 1% per experiment.
- **Decay and rollback** *(built for incident fixes)*: after a change, the same verifier as §7 compares the primary
  KPI and the guardrail with the period before the change; if either fell significantly, it proposes a revert.
  For winners I'd run it on a rolling 2-day window.
- **Call time and caller ID** *(not built)*: per cohort hour weights from history, then per-person only for returning
  people with ≥ 3 past attempts. Caller ID: pickup per DID vs the workflow's other DIDs, two-proportion test,
  Bonferroni over DIDs; move weight off a bad DID within `max_shift` (auto-apply is allowed for caller_id), and send
  a small share back to probe recovery.
- **Guardrail KPI** *(built)*: verification reads `objective` from the policy. The primary may not fall by more than
  10% relative, the guardrail by more than its `*_max_drop` (absolute), each with one-sided p < 0.01, compared with the
  24 h before the problem started. A fix that lowers hallucinations but hurts engagement is reverted.

## 6. Governance

**State machine** (the `status` column of `proposals`; see `governance.py` docstring):

```
intent (open) ──governance tick──> proposal
  approval route:  sending ─POST ok─> pending ─approved─> approved ─apply ok─> applied
                                              ─rejected─> rejected  (intents dropped; 24 h cool-down, 2 h if urgent)
                                              ─expired──> expired   (intents re-opened; NEW proposal on fresh config)
                                                          approved ─410─> lapsed  (re-opened)
                                                          approved ─409─> stale   (re-opened, re-proposed on the new base)
  auto route:      auto_sending ─PUT ok─> applied
                                ─403───> forbidden (intents re-routed to approval)
                                ─409───> stale     (re-opened)
  crash before the POST/PUT reached the simulator:  sending/auto_sending ─reconcile─> abandoned (rebuilt)
```

- **Batching and timing:** one open proposal per workflow at a time; every open intent for that workflow is merged into
  it on top of the *latest* config. A normal request is sent only inside `approver_hours`, not in the first hour, only if
  average latency fits before the day ends, and only within the budget: `min(max_requests_per_24h − 1, 2)` normal
  requests per 24 h, keeping one in reserve for urgent ones. An urgent request (incident fix) may use the whole cap and
  any approver hour. Requests expire after the policy's `proposal_ttl_hours`. While a decision is pending the loop
  advances 15 minutes at a time, so an approved change is applied well inside `apply_window_hours`.
- **Auto-apply:** used only when the policy allows it (enabled, every changed dim in `allowed_dims`, total-variation
  shift ≤ `max_shift`) **and** the change is operational (caller_id, schedule). I voluntarily don't auto-apply welcome
  or voice on Kestrel, even though the policy allows it: the client's own supervisor asked to be told first, and a
  customer-facing change is what compliance cares about.
- **Impact score, affected leads, dry run:**
  - *affected_leads*: leads in the latest day's campaign that fall in the changed cohorts (all leads for a
    workflow-wide change), from our own lead counts.
  - *impact_score* = 0.5 × severity + 0.5 × share of the workflow's leads affected. Severity is set by the detector:
    1.0 for a confirmed hallucination incident. It's a ranking aid for the approver's queue, not a forecast of lift,
    and I say so.
  - *dry_run*: the evidence the decision was made on — counts on the bad prompt vs history, the chosen prompt and
    every candidate's validation pass rate. It isn't a counterfactual replay (C10 partial).
- **Supervisor edits mid-flight:** ingested as `supervisor_edit` audit rows. An open intent on the same dimension is
  dropped (her edit wins). Any proposal in flight was built on an older base, so its apply gets 409, and the
  remaining intents are rebuilt on her version. An incident flow whose prompt she changed stops (`incident_superseded`).

## 7. Incidents

- **Detection:** per workflow, on the prompt that is live now. Recent windows of 2, 6 and 24 h on that prompt only
  (so a long window never mixes in an older prompt). Fires when, for some window, **connected ≥ 200**, **rate > 2× the
  history rate**, and the **exact binomial tail** P(X ≥ x | n, p_hist) < 0.001 / (24 × 3).
  - *Why exact binomial*: at 200 calls and a 1.4% base rate we expect ~3 hallucinations; the normal approximation is
    badly anti-conservative there. A z-test produced a false incident on public_01 (8 of 206 calls: z-test
    p = 0.0009, exact p = 0.0068). It's a regression test now.
  - *False-positive rate*: Bonferroni spends α = 0.001 over one day's looks (24 hourly × 3 windows), so ≈ 1 false
    incident per workflow per 1,000 days. public_01: zero.
- **Picking a fix:** `POST /llm/suggest-fixes` once (3 credits). Then validation with n = 400 (4 credits each) for the
  known-good prompt and each suggestion. Each result is saved before the next call. Only candidates with
  pass_rate ≥ `min_validation_pass_rate` qualify. The **known-good** prompt wins if it qualifies, because validation says
  nothing about engagement; otherwise the highest pass rate wins. Fixed rule, never the LLM. The request is urgent and
  carries `evidence.validation_run_id`.
- **Credit spend:** 15 per incident (3 + 3 × 4); 0 without an incident. Out of credits mid-flow → propose the best candidate that already passed validation (the known-good prompt is validated first, so usually that one); an ALERT only if none has passed. Never a guess.
- **Verification:** once the new prompt has 200 connected calls: re-run the incident test, and check the primary KPI and
  the guardrail against the 24 h before the bad prompt went live (§5). If either fails → propose the known-good prompt
  (if it passed validation) or raise an ALERT.
- **public_02 timeline (ks_cancel):**

| Sim time | What happened | Source |
|---|---|---|
| D3 11:30 | Supervisor ships P2 (payment-urgency claims) | `supervisor_edit` audit row |
| D3 19:00 | Detected: 4.2% vs 1.4% history over 504 connected calls, p = 1.0e-05 | `incident_detected` |
| D3 19:00 | LLM suggests P3, P1, P4; validated P1 98.5%, P3 99.3%, P4 97.3%; P1 chosen (known-good) | `llm_suggested`, `validated` |
| D3 19:00 – D4 10:00 | Waiting: outside approver hours (10–19 h) | — |
| D4 10:00 | Urgent request sent with validation evidence | `propose` |
| D4 10:42 | Kestrel's approver approves | `approval_decided` |
| D4 11:00 | Applied (v3) | `config_applied` |
| D4 15:00 | Verified: hallucination 1.5%; engagement and pickup not worse than before the bad prompt | `incident_resolved` |

MTTR 1410 min = ~7.5 h to collect a population + ~15 h waiting for the approver's morning + ~1 h to apply. The approver's
night is the biggest part; the policy doesn't allow auto-applying prompts, and I wouldn't want it to.

## 8. Scale

Measured with `scripts/load_test.sh` (full controller: ingest + detectors + governance) on this laptop
(8 CPUs, 7 GB RAM; the simulator runs on the same machine and generates the events):

| | Wall clock | Events | Events/s |
|---|---|---|---|
| History (7 days) + D0, on first start | 57.2 s | 6,377,391 | 111,493 |
| **D1, one steady-state simulated day** | **20.6 s** | 840,012 | 40,777 (limited by the simulator generating the day) |

Peak controller memory (VmHWM): **33 MB**. Ingest lag at every hourly decision: **0 events**. SRE target (< 600 s per
simulated day on 4 vCPU / 8 GB): **met, ~29× under**. Full output: `reports/load_public_03.txt`.

Ingest alone (`reports/c1_ingest_load_public03.txt`): 6.38 M events (history + D0) in 59 s, ~108k events/s, 32 MB.

- **Why memory stays flat:** chunks are streamed line by line and aggregated into hourly counters, so the store grows
  with (hours × config versions × cohorts × options), not with events. History is a single 6-million-event chunk and
  never sits in memory.
- **Decision lag:** decisions run after every advance on fully ingested data; the hourly log line shows the gap between
  the simulator's last event and our cursor (0 events at every hourly line on public_01, public_02 and public_03).
- **What breaks first at 10×** (~8.5 M events/day, hundreds of workflows):
  1. *The single process.* Ingest, detectors and governance share one thread, and the detector pass is one set of
     SQL queries per workflow per tick, so per-tick time grows linearly with workflows.
  2. *SQLite's single writer.* Fine for one process; doesn't allow several controllers to share a store.
  3. *Per-workflow HTTP calls* each tick (config, workflow policy) grow with workflows; they'd need caching with a
     version check.
- **How I'd partition:** shard by tenant (governance, approvers and credits are per tenant/workflow, so shards never
  coordinate). One ingester writes stats to Postgres partitioned by workflow; N decision workers each own a set of
  workflows, using a row lock on the workflow as the lease. Credit budgets per tenant (C15) become the only shared state.

## 9. Reliability

- **Crash test** (`scripts/crash_test.sh`, log in `reports/crash_test.log`): run public_02 and kill -9 the controller at
  sim minutes 3000, 5460 (incident detected, LLM and validation calls running), 6375 and 6405 (approval pending), 6500
  (just applied, verifying) and 9000. Restart after each; let it finish; grade; compare with the uninterrupted run.
  **Result: IDENTICAL to the uninterrupted run: outcomes, applied changes, governance counters, MTTR (1410) and credits (15); the incident still ends `incident_resolved`.** Store invariants: no approval id recorded twice, exactly one apply, one incident flow, nothing
  left in flight.
- **What can be lost:** nothing that changes a decision. If the kill lands between an LLM/validation HTTP call and
  saving its result, that call is repeated after restart: at most one call, ≤ 4 credits. I accepted that rather than
  add an idempotency key the simulator doesn't support for those endpoints.
- **Idempotency keys:**
  - *Events*: one transaction per chunk (stats + audit + approvals + cursor); a replayed chunk is skipped by number,
    and events at or below the cursor are skipped by id.
  - *Proposals*: `proposal_id = sha256(workflow | base_version | canonical config | attempt time)[:16]`, written
    **before** the HTTP call. A retried POST gets 409 duplicate → we look up the existing approval instead of
    creating another.
  - *Applies*: only from `approved`, and `applied` is set from the simulator's own config-history event, so a repeated
    apply attempt is detected (409/`not_approved` with status applied) and recorded once.
- **4xx/5xx:** the client retries 5xx and connection errors with exponential backoff (6 tries), and never retries a
  4xx. 4xx are typed: 402 credits exhausted → ALERT; 403 auto-apply forbidden → route to approval; 409 stale base →
  re-propose on fresh data; 409 duplicate → look up; 410 lapsed → re-open; 409 finished → stop cleanly. A chunk that
  fails mid-stream is re-read whole by ingest; nothing was committed.

## 10. Audit & observability

**"Why did ks_cancel change at D4 11:00, who approved it, and what happened after?"**, from the store only:

```
$ python -m controller.explain --db runs/mine_public_02_incidents.db --workflow ks_cancel --at "D4 12:00"
# ks_cancel at D4 12:00 (2027-01-08T12:00:00+05:30)
Live config: v3 since D4 11:00 (changed prompt_version by controller_approved)

## What and why (proposal p_c65374aa4c9086b9, route approval)
Summary: switch prompt P2 -> P1 to stop a hallucination spike
Rationale: Hallucinations on P2: 4.2% vs 1.4% baseline over 504 connected calls (p=1.0e-05). P1 passed validation at 98.5% (n=400, run val_00001). P1 is the last known-good prompt, so its engagement is known from history.
Diff: {"prompt_version": {"from": "P2", "to": "P1"}}
affected_leads: 1500
impact_score: 1.0
evidence: {"validation_run_id": "val_00001"}
dry_run: {"incident_prompt": {"bad_prompt": {"connected": 504, "dials": 3000, "engaged": 239, "halluc": 21, "prompt": "P2"}, "baseline_history": {"connected": 16439, "dials": 65018, "engaged": 8288, "halluc": 225}, "chosen": "P1", "validation_pass_rates": {"P1": 0.985, "P3": 0.9925, "P4": 0.9725}}}

## Who approved
proposed D4 10:00 against v2
D4 10:42 approver (apr_00001): approver: approved
applied D4 11:00
  context D3 19:00 incident_detected: hallucination rate 4.2% on P2 vs 1.4% baseline (504 connected calls, p=1.0e-05)
  context D3 19:00 incident_propose: fix: switch prompt P2 -> P1 to stop a hallucination spike

## What happened next (24 h before vs 24 h after; pickup and engagement kept separate)
                  pickup  engagement  hallucination   dials
before             17.3%       46.6%          4.10%    4800
after              16.2%       51.7%          1.03%    4800
  D4 12:00 incident_applied: prompt P1 is live; verifying
  D4 15:00 incident_resolved: fixed: hallucination 1.5% on P1 (was 4.2%), engagement 53.4%, pickup 17.2% (before the bad prompt: 50.4%, 16.5%)
```

The same answer with plain SQL, for anyone without the tool:

```sql
SELECT a.sim_ts, a.kind, a.actor, a.summary, p.payload_json
FROM audit a LEFT JOIN proposals p ON p.proposal_id = a.proposal_id
WHERE a.workflow_id = 'ks_cancel' AND a.sim_ts BETWEEN '2027-01-07T11' AND '2027-01-09'
ORDER BY a.id;
```

`python -m controller.explain --view` is the supervisor's one screen (and shows ingest lag and credits left).

**Would page (each message says what to look at):**
- `incident_unresolved`: a fix was applied and failed, and there's no validated prompt left — a human must choose.
- `incident_no_credits`: credits ran out mid-incident before any candidate passed validation.
- `incident_no_fix`: no candidate prompt passed validation.
- Ingest lag growing for 3 hours in a row, or the controller not writing its hourly health record for 2 hours.
- Repeated 5xx after all retries (the loop stops advancing).

**Would not page (dashboard / morning summary):** an incident detected and being handled; an approval rejected, expired
or lapsed (re-proposed automatically); a supervisor edit; a stale-base re-proposal; a single 5xx that a retry fixed.

## 11. Results

**public_01_dev** (`reports/public_01_dev.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.333 | +0.0% | 0.238 | +0.0% | 0.491 | +0.0% | 0% | 47 (47) |
| nw_kyc | 0.364 | +0.0% | 0.242 | +0.0% | 0.591 | +0.0% | 0% | 21 (21) |
| nw_renewal | 0.420 | +0.0% | 0.263 | +0.0% | 0.546 | +0.0% | 0% | 42 (42) |
| ALL | 0.368 | +0.0% | 0.248 | +0.0% | 0.530 | +0.0% | 0% | 110 (110) |

## Changes applied by the controller

0 applied —

Approval requests: 0 · Credits: Credits used: 0 · validation runs: 0 · LLM fix calls: 0

**public_02_incidents** (`reports/public_02_incidents.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.259 | +4.6% | 0.183 | -0.2% | 0.497 | +4.3% | 25% | 77 (330) |
| nw_kyc | 0.378 | +0.9% | 0.241 | -0.6% | 0.601 | +1.4% | 0% | 31 (31) |
| nw_renewal | 0.431 | +1.2% | 0.263 | +0.4% | 0.552 | +0.7% | 5% | 70 (55) |
| ALL | 0.342 | +2.2% | 0.222 | -0.1% | 0.541 | +2.1% | 12% | 178 (416) |

## Changes applied by the controller

1 applied — beneficial: 1

- ks_cancel v3 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**

Approval requests: 1 · Credits: Credits used: 15 · validation runs: 3 · LLM fix calls: 1 · MTTR 1410 min

**Interpretation:**
- **Where I helped:** ks_cancel. The bad supervisor prompt was replaced by the known-good one: hallucinated calls 330 →
  77, engagement (the primary KPI) +4.3%, conclusion rate +4.6%, pickup (guardrail) −0.2%, inside its 2-point limit.
  The one applied change is graded beneficial.
- **Where I was harmful:** no applied change was harmful. But **where I did nothing was costly**: the flagged caller ID
  on ks_cancel from D2 stayed at ~50% of traffic for the whole run, because C5 isn't built. That's the first thing I'd
  add.
- **The other workflows** move by about ±1% with no change from us: that's the run-to-run noise of the simulator, and a
  useful reminder of why one-call or one-day differences aren't evidence. nw_renewal's hallucinated calls (70 vs 55)
  come from the supervisor's own voice edit on D5 15:00, which we recorded and left alone.
- **What I'd change:** MTTR is dominated by the approver's night (15 of 23.5 h). I can't move that without breaking
  governance, but I could detect sooner on busy workflows by allowing a 1 h window.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Postgres / Redis | SRE already runs them | No new service for a take-home; SQLite is a file with the same SQL. Move to their Postgres for production (§14). |
| Store every event | Simplest audit | 850k events/day × hundreds of workflows. Hourly counters per config version keep what decisions need; config history + audit answer "what was live". |
| z-test for incidents | Standard, simple | False alarm on public_01 at small counts; exact binomial instead. |
| Let the LLM pick the fix | It has an opinion | Hard rule, and validation is the evidence. Fixed rule: known-good if it passes, else best pass rate. |
| Pick the highest validation pass rate | Fewest hallucinations | Validation doesn't measure engagement. P3 scored highest, but P1's engagement is known. Anita's "word-for-word script" warning is exactly this failure. |
| Compare post-fix KPIs with 7-day history | Large sample | Pickup had already dropped on ks_cancel for another reason (flagged DID); it falsely blamed the fix. Use the 24 h before the bad prompt. |
| Auto-apply welcome/voice where allowed | Faster | Customer-facing; both client memos ask for a human. |
| One request per change | Simpler | Approver fatigue; batching per workflow, capped at 2/day. |
| Wait for 11:00 for urgent fixes too | Anita asked | One more hour of hallucinating calls; urgent fixes may go at 10:00 and I tell her. |
| Experiments with a bandit | Faster convergence | Harder to explain to compliance and to bound false winners; fixed-horizon tests first. Not built anyway. |
| Advance 15 min always | Fresher decisions | 4× more HTTP round trips; 15 min only while a decision is pending. |

## 13. Known weaknesses

1. **No experiments, no caller-ID health, no call-time work.** Outside incidents the controller makes no changes, so
   there's no primary-KPI lift from learning on the hidden scenarios. The flagged DID on public_02 stayed in rotation.
2. **Detection is per workflow, not per cohort.** A spike confined to a small cohort is diluted and may be caught late.
3. **The history baseline is assumed healthy.** A workflow that was already hallucinating in history would look normal.
4. **When the known-good prompt fails validation**, the highest pass rate wins, which could be a strict near-verbatim
   script. Verification would catch the engagement drop and alert, but only after 200 connected calls had heard it.
5. **Verification compares before and after, not a concurrent control.** A day-of-week or campaign-mix shift could
   look like a KPI drop (mitigated by significance + a 24 h reference, not eliminated).
6. **Per-customer audit** ("what did this person hear") is answered per workflow, config version and cohort, not per call.
7. **One repeated LLM or validation call** (≤ 4 credits) is possible after a crash at the wrong instant.
8. **Single process**; see §8 for the limits.
9. **Thresholds (200 calls, 2×, 10%, α) are my choices**, reasoned but not tuned on data beyond the public scenarios.
10. **Two bugs I found in my own code late** (a placeholder rationale shown to the approver, and the history-based
    guardrail false alarm above) — both fixed with regression tests, but they show where the test suite was thin.

## 14. If this went to production at Formi next month

1. **Week 1, day 1–2:** move the store to the platform Postgres (same schema), on a persistent volume until then; run the
   controller in **shadow mode** on a few real workflows: it files proposals to a log, applies nothing.
2. **Day 3–4:** wire the paging list (§10) and the hourly health record to the existing alerting; a dashboard for lag,
   decisions/hour and credit burn.
3. **Day 5:** review a week of shadow incidents with the client's ops supervisors; calibrate thresholds on real false
   alarms.
4. **Week 2:** build **C5 caller-ID health** (operational, auto-applicable within policy, measurable on pickup) and turn
   on incident response + C5 for one tenant with approval.
5. **Week 2, end:** shard by tenant (§8); per-tenant credit budgets (C15); the weekly change summary Northwind asked for.
6. Then experiments (C6/C7), starting with one dimension, one cohort, fixed horizon.
