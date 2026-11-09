"""Client retry and error-mapping behaviour, against a fake transport (no simulator needed)."""
import httpx
import pytest

from controller import client as c


def make(handler):
    return c.SimClient("http://sim", backoff=0, max_retries=3, transport=httpx.MockTransport(handler))


def test_retries_5xx_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert make(handler).get("/health") == {"ok": True}
    assert len(calls) == 3


def test_retries_connection_errors():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"ok": True})

    assert make(handler).get("/health") == {"ok": True}


def test_gives_up_after_max_retries():
    with pytest.raises(httpx.HTTPStatusError):
        make(lambda req: httpx.Response(500)).get("/health")


@pytest.mark.parametrize("status,code,exc", [
    (402, "out_of_credits", c.CreditsExhausted),
    (403, "auto_apply_not_permitted", c.AutoApplyForbidden),
    (409, "stale_base_version", c.StaleBase),
    (409, "duplicate_proposal_id", c.DuplicateProposal),
    (409, "finished", c.Finished),
    (409, "not_approved", c.NotApproved),
    (410, "approval_lapsed", c.Lapsed),
    (422, "missing_fields", c.SimError),
])
def test_4xx_is_typed_and_never_retried(status, code, exc):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status, json={"error": code, "detail": {"current_version": 7}})

    with pytest.raises(exc) as e:
        make(handler).post("/approvals", {})
    assert len(calls) == 1
    assert e.value.code == code and e.value.status == status


def test_iter_chunk_parses_ndjson():
    body = b'{"event_id": 1}\n\n{"event_id": 2}\n'
    events = list(make(lambda req: httpx.Response(200, content=body)).iter_chunk(0))
    assert [e["event_id"] for e in events] == [1, 2]
