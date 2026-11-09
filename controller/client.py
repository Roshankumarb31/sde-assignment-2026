"""HTTP client for the simulator. The only way the controller touches the world.

Two kinds of failure, handled very differently:
- Transient (5xx, connection errors, timeouts): retried with exponential backoff. Safe because every
  write we send carries an idempotency key (proposal_id), and reads have no side effects.
- 4xx: never retried blindly. Each one means "your picture of the world is wrong" or "the rules say no",
  so it is raised as a named exception and the caller decides (refetch, re-propose, fall back, stop).

The simulator's error body is {"error": <code>, "detail": <anything>}.
"""
import json
import time

import httpx


class SimError(Exception):
    """Any 4xx from the simulator that has no more specific class."""
    def __init__(self, status, code, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class CreditsExhausted(SimError):      # 402 out_of_credits
    pass


class AutoApplyForbidden(SimError):    # 403 auto_apply_not_permitted (detail = list of reasons)
    pass


class StaleBase(SimError):             # 409 stale_base_version (detail.current_version)
    pass


class DuplicateProposal(SimError):     # 409 duplicate_proposal_id: we already sent this one
    pass


class Finished(SimError):              # 409 finished: scenario is over
    pass


class NotApproved(SimError):           # 409 not_approved (detail.status, e.g. already applied)
    pass


class Lapsed(SimError):                # 410 approval_lapsed: missed apply_by
    pass


_BY_CODE = {
    "out_of_credits": CreditsExhausted,
    "auto_apply_not_permitted": AutoApplyForbidden,
    "stale_base_version": StaleBase,
    "duplicate_proposal_id": DuplicateProposal,
    "finished": Finished,
    "not_approved": NotApproved,
    "approval_lapsed": Lapsed,
}


class SimClient:
    def __init__(self, base_url, timeout=120.0, max_retries=6, backoff=0.5, transport=None):
        # transport is only for tests (httpx.MockTransport)
        self.http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self.max_retries = max_retries
        self.backoff = backoff

    def close(self):
        self.http.close()

    # ------------------------------------------------------------ core

    def _send(self, method, path, **kw):
        """Send with retry on transient failures; raise a typed SimError on 4xx."""
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.request(method, path, **kw)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == self.max_retries:
                    raise
            else:
                if r.status_code < 400:
                    return r
                if r.status_code < 500:
                    raise _to_error(r)
                if attempt == self.max_retries:
                    r.raise_for_status()
            time.sleep(self.backoff * 2 ** attempt)  # 0.5, 1, 2, 4, 8, 16s

    def get(self, path, **params):
        return self._send("GET", path, params=params or None).json()

    def post(self, path, body):
        return self._send("POST", path, json=body).json()

    def put(self, path, body):
        return self._send("PUT", path, json=body).json()

    # ------------------------------------------------------------ endpoints

    def state(self):
        return self.get("/sim/state")

    def advance(self, minutes):
        return self.post("/sim/advance", {"minutes": minutes})

    def list_chunks(self, kind, after=-1):
        return self.get("/events/chunks", kind=kind, after=after)

    def iter_chunk(self, n):
        """Stream one chunk's events, one parsed line at a time, so memory stays flat even for the
        history chunk (millions of events on the load scenario).

        No retry in here: if the connection drops mid-stream the exception reaches ingest, which
        throws away the half-built delta (nothing was committed) and fetches the chunk again."""
        with self.http.stream("GET", f"/events/chunks/{n}") as r:
            if r.status_code >= 400:
                r.read()
                if r.status_code < 500:
                    raise _to_error(r)
                r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    yield json.loads(line)

    def workflows(self):
        return self.get("/workflows")

    def workflow(self, wid):
        return self.get(f"/workflows/{wid}")

    def config(self, wid):
        return self.get(f"/workflows/{wid}/config")

    def auto_apply(self, wid, body):
        return self.put(f"/workflows/{wid}/config", body)

    def request_approval(self, body):
        return self.post("/approvals", body)

    def approvals(self, workflow_id=None, status=None):
        params = {k: v for k, v in {"workflow_id": workflow_id, "status": status}.items() if v}
        return self.get("/approvals", **params)

    def approval(self, approval_id):
        return self.get(f"/approvals/{approval_id}")

    def apply(self, wid, approval_id):
        return self.post(f"/workflows/{wid}/apply", {"approval_id": approval_id})

    def suggest_fixes(self, wid, problem):
        return self.post("/llm/suggest-fixes", {"workflow_id": wid, "problem": problem})

    def validate(self, wid, prompt_version, n):
        return self.post("/validation/runs", {"workflow_id": wid, "prompt_version": prompt_version, "n": n})


def _to_error(r):
    try:
        body = r.json()
        code, detail = body.get("error") or "http_error", body.get("detail")
    except ValueError:
        code, detail = "http_error", r.text
    return _BY_CODE.get(code, SimError)(r.status_code, code, detail)
