import asyncio
import json
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from test_integration import auth, exchange, stack  # noqa: F401

from gateway.db import one, rows, run
from gateway.fake_atlassian import app as fake
from gateway.errors import GatewayError
from gateway.app import create_app


@pytest_asyncio.fixture
async def mcp_stack(stack):  # noqa: F811
    app, c, s, identity, provider = stack
    s.jira_base_url, s.confluence_base_url = "http://fake/jira", "http://fake/confluence"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake)) as upstream:
        app.state.mcp.atlassian.client = upstream
        yield stack


async def setup(c, h):
    session = await c.post("/auth/mcp/setup", headers=h)
    assert session.status_code == 200
    result = session.json()
    return result, {"Authorization": "Bearer " + urlsplit(result["url"]).fragment, "Origin": "http://localhost:18000"}


async def save(c, h, **changes):
    session, sh = await setup(c, h)
    body = changes or {"jira": {"action": "replace", "token": "fake-jira-pat"},
                       "confluence": {"action": "replace", "token": "fake-confluence-pat"}}
    result = await c.post("/auth/mcp/setup/session", headers=sh, json=body)
    assert result.status_code == 200, result.text
    return session, sh


async def rpc(c, h, system, method, params=None):
    r = await c.post("/mcp/" + system, headers={**h, "MCP-Protocol-Version": "2025-11-25"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


async def test_complete_onboarding_and_real_http_mcp_contract(mcp_stack):
    app, c, s, _, _ = mcp_stack
    login = await exchange(c)
    assert login.json()["mcp_setup_required"] is True
    h = auth(login)
    page = await c.get("/auth/mcp/setup")
    assert page.headers["cache-control"] == "no-store" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "fake-jira-pat" not in page.text
    session, sh = await save(c, h)
    poll = await c.get("/auth/mcp/setup/status/" + session["id"], headers=h)
    assert poll.json() == {"complete": True}
    assert (await c.get("/auth/mcp/setup/session", headers=sh)).status_code == 401
    catalog = (await c.get("/mcp/catalog", headers=h)).json()
    assert len(catalog["servers"]) == 2 and all(item["connected"] for item in catalog["servers"])
    async with app.state.db.tx() as conn:
        saved = await rows(conn, "SELECT * FROM mcp_credentials")
        audits = await rows(conn, "SELECT * FROM audit_events")
    assert len(saved) == 2
    assert all("fake-" not in r["ciphertext"] for r in saved)
    assert "fake-jira-pat" not in json.dumps([dict(r) for r in audits], default=str)
    for system, name, arguments in [("jira", "search_issues", {"jql": "project = DEMO"}),
            ("jira", "get_issue", {"key": "DEMO-1"}), ("confluence", "search_pages", {"cql": 'space = "DEMO"'}),
            ("confluence", "get_page", {"id": "1001"})]:
        initialize = await rpc(c, h, system, "initialize", {"protocolVersion": "2025-06-18"})
        assert initialize["result"]["protocolVersion"] == "2025-06-18"
        listed = await rpc(c, h, system, "tools/list")
        assert len(listed["result"]["tools"]) == 2
        result = await rpc(c, h, system, "tools/call", {"name": name, "arguments": arguments})
        assert result["result"]["isError"] is False
        assert "FAKE" in result["result"]["content"][0]["text"]
        assert "fake-jira-pat" not in json.dumps(result)
    assert (await c.get("/mcp/jira", headers=h)).status_code == 405
    note = await c.post("/mcp/jira", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202 and not note.content


async def test_isolation_skip_remove_and_revocation(mcp_stack):
    app, c, _, _, _ = mcp_stack
    a, b = await exchange(c), await exchange(c)
    ah, bh = auth(a), auth(b)
    session, sh = await save(c, ah)
    assert (await c.get("/auth/mcp/setup/status/" + session["id"], headers=bh)).status_code == 410
    assert all(not item["connected"] for item in (await c.get("/mcp/catalog", headers=bh)).json()["servers"])
    assert (await rpc(c, bh, "jira", "tools/list"))["result"]["tools"] == []
    result = await rpc(c, bh, "jira", "tools/call", {"name": "get_issue", "arguments": {"key": "DEMO-1"}})
    assert result["result"]["isError"] and result["result"]["content"][0]["text"] == "mcp_pat_required_relogin"
    await save(c, bh, jira={"action": "keep"})
    await save(c, ah, jira={"action": "remove"}, confluence={"action": "keep"})
    servers = (await c.get("/mcp/catalog", headers=ah)).json()["servers"]
    assert servers[0]["connected"] is False and servers[1]["connected"] is True
    pending, pending_h = await setup(c, ah)
    assert (await c.delete("/auth/cli/credentials/current", headers=ah)).status_code == 204
    assert (await c.post("/auth/mcp/setup/session", headers=pending_h, json={})).status_code == 401
    assert (await c.post("/mcp/confluence", headers=ah, json={})).status_code == 401


async def test_bad_pat_origin_expiry_and_replay_do_not_mutate_credentials(mcp_stack):
    app, c, _, _, _ = mcp_stack
    h = auth(await exchange(c))
    session, sh = await setup(c, h)
    body = {"jira": {"action": "replace", "token": "fake-jira-pat"}, "confluence": {"action": "replace", "token": "INVALID_SECRET"}}
    denied = await c.post("/auth/mcp/setup/session", headers={**sh, "Origin": "https://evil.invalid"}, json=body)
    assert denied.status_code == 403
    rejected = await c.post("/auth/mcp/setup/session", headers=sh, json=body)
    assert rejected.status_code == 422 and "INVALID_SECRET" not in rejected.text
    assert all(not s["connected"] for s in (await c.get("/mcp/catalog", headers=h)).json()["servers"])
    async with app.state.db.tx() as conn:
        await run(conn, "UPDATE mcp_setup_sessions SET expires_at=now()-interval '1 second' WHERE id=:id", id=session["id"])
    assert (await c.post("/auth/mcp/setup/session", headers=sh, json={})).status_code == 401
    session, sh = await setup(c, h)
    results = await asyncio.gather(*(c.post("/auth/mcp/setup/session", headers=sh, json={}) for _ in range(2)))
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_endpoint_change_arguments_rate_limit_and_suspension(mcp_stack):
    app, c, s, _, _ = mcp_stack
    login = await exchange(c)
    h = auth(login)
    await save(c, h)
    result = await rpc(c, h, "jira", "tools/call", {"name": "get_issue", "arguments": {"key": "../../myself"}})
    assert result["result"]["isError"] and "mcp_invalid_arguments" in result["result"]["content"][0]["text"]
    result = await rpc(c, h, "jira", "tools/call", {"name": "delete_issue", "arguments": {}})
    assert "mcp_tool_not_found" in result["result"]["content"][0]["text"]
    s.jira_base_url = "http://other/jira"
    assert (await rpc(c, h, "jira", "tools/list"))["result"]["tools"] == []
    result = await rpc(c, h, "jira", "tools/call", {"name": "get_issue", "arguments": {"key": "DEMO-1"}})
    assert "mcp_pat_required_relogin" in result["result"]["content"][0]["text"]
    s.mcp_request_rate = 1
    result = await rpc(c, h, "confluence", "tools/call", {"name": "get_page", "arguments": {"id": "1001"}})
    assert "mcp_rate_limited" in result["result"]["content"][0]["text"]
    await app.state.auth.suspend(login.json()["principal"]["id"], "test-mcp")
    assert (await c.get("/mcp/catalog", headers=h)).status_code in {401, 403}
    async with app.state.db.tx() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM mcp_call_leases"))["n"] == 0


async def test_mcp_login_does_not_allocate_inference_and_preserves_budget(mcp_stack):
    app, c, s, _, provider = mcp_stack
    s.provision_inference_on_login = False
    async with app.state.db.tx() as conn:
        await run(conn, "UPDATE policies SET provider_budget=0 WHERE id='default'")
    login = await exchange(c)
    h, owner = auth(login), login.json()["principal"]["id"]
    assert provider.created == 0
    await save(c, h)
    result = await rpc(c, h, "jira", "tools/call", {"name": "get_issue", "arguments": {"key": "DEMO-1"}})
    assert result["result"]["isError"] is False
    with pytest.raises(GatewayError, match="principal_provider_budget_exhausted"):
        await app.state.broker.inference_secret(owner)
    assert provider.created == 0
    async with app.state.db.tx() as conn:
        p = await one(conn, "SELECT provider_allocated FROM principals WHERE id=:id", id=owner)
        assert float(p["provider_allocated"]) == 0
        await run(conn, "UPDATE policies SET provider_budget=.25 WHERE id='default'")
    assert await app.state.broker.inference_secret(owner)
    assert provider.created == 1
    await app.state.broker.inference_secret(owner)
    assert provider.created == 1


async def test_two_replicas_share_mcp_concurrency_quota(mcp_stack):
    app, c, s, identity, provider = mcp_stack
    h = auth(await exchange(c))
    await save(c, h)
    s.mcp_request_concurrency = 1
    started, release = asyncio.Event(), asyncio.Event()
    original = app.state.mcp.atlassian.call

    async def blocked(*args):
        started.set()
        await release.wait()
        return await original(*args)
    app.state.mcp.atlassian.call = blocked
    replica = create_app(s, identity, provider, worker=False)
    replica.state.mcp.atlassian.client = app.state.mcp.atlassian.client
    async with replica.router.lifespan_context(replica), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica), base_url=s.gateway_public_url) as other:
        first = asyncio.create_task(rpc(c, h, "jira", "tools/call", {"name": "get_issue", "arguments": {"key": "DEMO-1"}}))
        try:
            await asyncio.wait_for(started.wait(), 5)
            denied = await rpc(other, h, "confluence", "tools/call", {"name": "get_page", "arguments": {"id": "1001"}})
            assert denied["result"]["isError"] and "mcp_concurrency_limited" in denied["result"]["content"][0]["text"]
        finally:
            release.set()
            assert (await first)["result"]["isError"] is False
        allowed = await rpc(other, h, "confluence", "tools/call", {"name": "get_page", "arguments": {"id": "1001"}})
        assert allowed["result"]["isError"] is False
