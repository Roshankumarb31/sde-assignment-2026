# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.259 | +4.6% | 0.183 | -0.2% | 0.497 | +4.3% | 25% | 77 (330) |
| nw_kyc | 0.378 | +0.9% | 0.241 | -0.6% | 0.601 | +1.4% | 0% | 31 (31) |
| nw_renewal | 0.431 | +1.2% | 0.263 | +0.4% | 0.552 | +0.7% | 5% | 70 (55) |
| ALL | 0.342 | +2.2% | 0.222 | -0.1% | 0.541 | +2.1% | 12% | 178 (416) |

## Changes applied by the controller

1 applied — beneficial: 1

- ks_cancel v3 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 1,
  "decisions": {
    "approved": 1,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 1,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.509,
      "3": 0.502,
      "4": 0.488,
      "5": 0.497,
      "6": 0.502,
      "7": 0.486,
      "8": 0.5,
      "9": 0.51
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1410,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 15 · validation runs: 3 · LLM fix calls: 1