"""C9 evidence: drive the three governance routes against a running simulator (public_02 recommended).

    python -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/gov --force --port 8750
    PYTHONPATH=. python scripts/governance_live_check.py --url http://127.0.0.1:8750 --db /tmp/gov.db

At D0 09:00 it files three hand-made intents (not real detections) and runs the loop for 60 sim hours:
  nw_renewal caller_id  operational, policy allows auto-apply      -> auto-applied
  nw_kyc     voice      auto-apply disabled for this workflow      -> approval request
  ks_cancel  voice      policy WOULD allow auto-apply for voice    -> approval request anyway (customer-facing)
"""
import argparse

from controller import governance as g
from controller import store
from controller.client import SimClient
from controller.ingest import Ingestor

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True)
ap.add_argument("--db", required=True)
a = ap.parse_args()
conn, c = store.open_db(a.db), SimClient(a.url)
ing, gv = Ingestor(conn, c), g.Governance(conn, c)
ing.sync()
for h in range(60):
    now = c.advance(60)["ts"]
    ing.sync()
    if h == 8:  # D0 09:00
        g.add_intent(conn, now, "nw_renewal", "caller_id", [[["allocations", "default", "caller_id"], {"D1": 0.7, "D2": 0.3}]],
                     "shift caller-ID weight toward D1", "governance live check", 0.4, {"note": "hand-made intent"})
        g.add_intent(conn, now, "nw_kyc", "voice", [[["allocations", "default", "voice"], {"V1": 0.5, "V2": 0.5}]],
                     "split voice V1/V2 50:50", "governance live check", 0.3, {"note": "hand-made intent"})
        g.add_intent(conn, now, "ks_cancel", "voice", [[["allocations", "default", "voice"], {"V1": 0.9, "V2": 0.1}]],
                     "try voice V2 on 10% of calls", "governance live check", 0.2, {"note": "hand-made intent"})
    gv.tick(now)
for r in conn.execute("SELECT sim_ts, workflow_id, kind, actor, summary FROM audit WHERE workflow_id IS NOT NULL "
                      "AND kind != 'supervisor_edit' ORDER BY id"):
    print(r["sim_ts"][5:16], f"{r['workflow_id']:<11}", f"{r['kind']:<16}", f"{r['actor']:<20}", r["summary"][:90])
print()
for r in conn.execute("SELECT workflow_id, route, status, approval_id, created_at, applied_at FROM proposals"):
    print(dict(r))
