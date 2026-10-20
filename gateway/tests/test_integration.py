from gateway.schema import initialize_test_schema
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event

from gateway.app import create_app
from gateway.broker import uid
from gateway.config import Settings
from gateway.db import DB, one, rows, run
from gateway.errors import GatewayError
from gateway.inference import Usage
from providers.base import Capabilities, Provisioned


class TestIdentity:
    __test__ = False
    allowed = True
    outage = False

    async def verify(self, token):
        if token == "denied":
            raise GatewayError("role_required", 403)
        return {"sub": token}

    async def discover(self):
        return {}

    async def status(self, sub):
        if self.outage:
            raise GatewayError("identity_status_unavailable")
        return self.allowed


class TestProvider:
    __test__ = False

    def __init__(self):
        self.keys, self.created, self.revoked, self.lost, self.revoke_fails = {}, 0, [], False, False

    def capabilities(self):
        return Capabilities(True, True)

    async def provision(self, label, limit, expires_at):
        self.created += 1
        await asyncio.sleep(0.02)
        ref = uid("ref_")
        self.keys[ref] = {"label": label, "key": uid("UPSTREAM_SECRET_")}
        if self.lost:
            raise GatewayError("provider_management_unavailable")
        return Provisioned(ref, self.keys[ref]["key"])

    async def inspect(self, ref):
        return {"disabled": ref not in self.keys}

    async def revoke(self, ref):
        if self.revoke_fails:
            raise GatewayError("provider_unavailable")
        self.keys.pop(ref, None)
        self.revoked.append(ref)

    async def find(self, label):
        return [k for k, v in self.keys.items() if v["label"] == label]


@pytest_asyncio.fixture
async def stack():
    s = Settings(model_catalog_file="", model_catalog=[], provider_mode="fake", provision_inference_on_login=True)
    if s.profile != "dev":
        pytest.skip("Integration requires isolated dev PostgreSQL; run make up/bootstrap")
    identity, provider = TestIdentity(), TestProvider()
    schema = "test_" + uuid4().hex
    setup = DB(s.database_url.get_secret_value())
    async with setup.tx() as c:
        await run(c, f'CREATE SCHEMA "{schema}"')
    s = s.model_copy(update={"database_schema": schema})
    app = create_app(s, identity, provider, worker=False)
    try:
        async with app.state.db.tx() as c:
            await initialize_test_schema(c, Path(__file__).resolve().parents[1] / "migrations")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=s.gateway_public_url) as client:
                yield app, client, s, identity, provider
    finally:
        async with setup.tx() as c:
            await run(c, f'DROP SCHEMA "{schema}" CASCADE')
        await setup.engine.dispose()


async def exchange(client, owner=None, idem=None, device="test-device"):
    return await client.post("/auth/cli/exchange", json={"device_name": device, "client": "kilo-cli"},
                            headers={"Authorization": "Bearer " + (owner or uid("subject_")),
                                     "Idempotency-Key": idem or uuid4().hex})


def auth(response):
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["credential"]["key"]}


async def test_two_replicas_idempotency_and_reuse(stack):
    app, client, s, ident, provider = stack
    second = create_app(s, ident, provider, worker=False)
    owner, idem = uid("subject_"), uuid4().hex
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url=s.gateway_public_url) as c2:
            results = await asyncio.gather(*(exchange(client if i % 2 else c2, owner, idem) for i in range(8)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["credential"]["key"] for r in results}) == 1
    assert provider.created == 1
    r2 = await exchange(client, owner)
    assert r2.status_code == 200 and r2.json()["credential"]["id"] != results[0].json()["credential"]["id"]
    assert provider.created == 1
    assert (await exchange(client, owner, idem, "changed")).status_code == 409
    async with app.state.db.tx() as c:
        await run(c, "UPDATE exchange_results SET expires_at=now()-interval '1 second' WHERE idem_key=:k", k=idem)
    assert (await exchange(client, owner, idem)).json()["error"]["code"] == "idempotency_result_expired"


async def test_role_denial_has_no_provisioning(stack):
    _, c, _, _, p = stack
    r = await exchange(c, "denied")
    assert r.status_code == 403 and p.created == 0


async def test_isolation_revocation_expiry_and_suspend(stack):
    app, c, s, _, p = stack
    a, b = await exchange(c), await exchange(c)
    ah, bh = auth(a), auth(b)
    aid = a.json()["credential"]["id"]
    assert (await c.delete("/auth/cli/credentials/" + aid, headers=bh)).status_code == 404
    data = (await c.get("/auth/cli/credentials", headers=ah)).json()["data"]
    assert [x["id"] for x in data] == [aid] and "secret_hash" not in json.dumps(data)
    assert (await c.delete("/auth/cli/credentials/current", headers=ah)).status_code == 204
    assert (await c.get("/me", headers=ah)).status_code == 401
    assert len(p.keys) == 2  # Device revoke does not delete shared upstream credentials.
    bid = b.json()["credential"]["id"]
    async with app.state.db.tx() as db:
        await run(db, "UPDATE gateway_credentials SET expires_at=now()-interval '1 second' WHERE id=:id", id=bid)
    assert (await c.get("/me", headers=bh)).status_code == 401
    d = await exchange(c)
    owner = d.json()["principal"]["id"]
    assert (await c.post(f"/admin/users/{owner}/suspend", headers=auth(d))).status_code == 403
    assert (await c.post(f"/admin/users/{owner}/suspend", headers={"Authorization": "Bearer "+s.admin_token.get_secret_value()})).status_code == 200
    assert (await c.get("/me", headers=auth(d))).status_code == 401
    await app.state.broker.tick()
    assert p.revoked


async def test_status_revocation_and_fail_closed(stack):
    app, c, s, ident, _ = stack
    r = await exchange(c)
    h, owner = auth(r), r.json()["principal"]["id"]
    ident.outage = True
    # Previously checked status remains valid only within its documented freshness window.
    assert (await c.get("/me", headers=h)).status_code == 200
    async with app.state.db.tx() as db:
        await run(db, "UPDATE principals SET authz_checked_at=now()-interval '61 seconds' WHERE id=:id", id=owner)
    assert (await c.get("/me", headers=h)).json()["error"]["code"] == "identity_status_unavailable"
    ident.outage, ident.allowed = False, False
    assert (await c.get("/me", headers=h)).status_code == 403
    async with app.state.db.tx() as db:
        assert (await one(db, "SELECT revoked_at FROM gateway_credentials WHERE id=:id", id=r.json()["credential"]["id"]))["revoked_at"]


async def test_lost_create_response_restart_reconciliation(stack):
    app, c, s, ident, p = stack
    owner = uid("subject_")
    p.lost = True
    assert (await exchange(c, owner)).status_code == 503 and p.created == 1
    assert (await exchange(c, owner)).status_code == 503 and p.created == 1
    # Simulate restart by constructing a new application with the same PostgreSQL and provider state.
    second = create_app(s, ident, p, worker=False)
    async with second.router.lifespan_context(second):
        async with second.state.db.tx() as db:
            await run(db, "UPDATE provisioning_operations SET updated_at=now()-interval '31 seconds' WHERE principal_id IN (SELECT id FROM principals WHERE subject=:s)", s=owner)
        await second.state.broker.tick()
    assert len(p.keys) == 0 and len(p.revoked) == 1
    p.lost = False
    assert (await exchange(c, owner)).status_code == 200 and p.created == 2


async def test_db_failure_after_create_compensates(stack):
    app, c, _, _, p = stack
    fired = False

    def fail(conn, cursor, statement, params, context, many):
        nonlocal fired
        if "INSERT INTO provider_credentials" in statement and not fired:
            fired = True
            raise RuntimeError("synthetic DB outage")
    event.listen(app.state.db.engine.sync_engine, "before_cursor_execute", fail)
    try:
        r = await exchange(c)
    finally:
        event.remove(app.state.db.engine.sync_engine, "before_cursor_execute", fail)
    assert r.status_code == 503 and p.created == 1 and not p.keys and len(p.revoked) == 1


async def test_rotation_and_durable_revoke_preserve_allocation(stack):
    app, c, s, _, p = stack
    r = await exchange(c)
    owner = r.json()["principal"]["id"]
    old = await app.state.broker.active(owner)
    p.revoke_fails = True
    new = await app.state.broker.ensure(owner, rotate=True)
    assert old["id"] != new["id"] and new["generation"] == 2 and len(p.keys) == 2
    await app.state.broker.tick()
    async with app.state.db.tx() as db:
        job = await one(db, "SELECT * FROM durable_jobs WHERE credential_id=:id", id=old["id"])
        assert job["attempts"] == 1 and job["state"] == "pending"
        allocation = await one(db, "SELECT provider_allocated FROM principals WHERE id=:id", id=owner)
        assert float(allocation["provider_allocated"]) == 2*s.openrouter_test_key_limit_usd
        await run(db, "UPDATE durable_jobs SET retry_at=now() WHERE id=:id", id=job["id"])
    p.revoke_fails = False
    await app.state.broker.tick()
    assert len(p.keys) == 1 and old["provider_reference"] in p.revoked


async def test_atomic_reservations_limits_and_no_budget_reset(stack):
    app, c, _, _, _ = stack
    r = await exchange(c)
    owner, cred = r.json()["principal"]["id"], r.json()["credential"]["id"]
    results = await asyncio.gather(*(app.state.inference.reserve(owner, cred, "company-coder", 10000, uid("req_"))
                                   for _ in range(10)), return_exceptions=True)
    assert sum(x is None for x in results) == 3
    assert all(isinstance(x, GatewayError) and x.status == 429 for x in results if x is not None)
    with pytest.raises(GatewayError, match="rotation_wait"):
        await app.state.broker.ensure(owner, rotate=True)
    async with app.state.db.tx() as db:
        await run(db, "UPDATE usage_records SET outcome='disconnected' WHERE principal_id=:p", p=owner)
        records = await rows(db, "SELECT charged_units FROM usage_records WHERE principal_id=:p", p=owner)
    assert sum(x["charged_units"] for x in records) == 30000
    # Exercise the real asyncpg completion query, including a NULL usage report.
    async with app.state.db.tx() as db:
        rid = (await one(db, "SELECT id FROM usage_records WHERE principal_id=:p LIMIT 1", p=owner))["id"]
    await app.state.inference.finish(rid, Usage(), "disconnected")
    await app.state.broker.ensure(owner, rotate=True)
    me = (await c.get("/me", headers=auth(r))).json()
    assert me["usage"]["charged_units"] == 30000


async def test_principal_policy_rate_daily_and_model_isolation(stack):
    app, c, _, _, _ = stack
    a, b = await exchange(c), await exchange(c)
    owner, credential = a.json()["principal"]["id"], a.json()["credential"]["id"]
    policy = uid("policy_")
    async with app.state.db.tx() as db:
        await run(db, """INSERT INTO policies SELECT :id,ARRAY[]::text[],providers,1,3,150,200,provider_budget
            FROM policies WHERE id='default'""", id=policy)
        await run(db, "UPDATE principals SET policy_id=:pol WHERE id=:p", pol=policy, p=owner)
    assert (await c.get("/v1/models", headers=auth(a))).json()["data"] == []
    assert (await c.get("/v1/models", headers=auth(b))).json()["data"]
    with pytest.raises(GatewayError, match="model_not_allowed"):
        await app.state.inference.reserve(owner, credential, "company-coder", 100, uid("req_"))
    async with app.state.db.tx() as db:
        await run(db, "UPDATE policies SET aliases=ARRAY['company-coder'] WHERE id=:id", id=policy)
    await app.state.inference.reserve(owner, credential, "company-coder", 100, uid("req_"))
    with pytest.raises(GatewayError, match="principal_rate_limited"):
        await app.state.inference.reserve(owner, credential, "company-coder", 10, uid("req_"))
    async with app.state.db.tx() as db:
        await run(db, "UPDATE policies SET rate_limit=100 WHERE id=:id", id=policy)
    with pytest.raises(GatewayError, match="principal_budget_exhausted"):
        await app.state.inference.reserve(owner, credential, "company-coder", 100, uid("req_"))


async def test_two_model_catalog_selection_and_permission_changes(stack):
    from gateway.catalog import read_catalog
    app, c, s, _, provider = stack
    s.model_catalog = read_catalog("builtin:y-ai")
    ids = [m.id for m in s.models]
    async with app.state.db.tx() as db:
        await run(db, "UPDATE policies SET aliases=:a WHERE id='default'", a=ids)
    issued = await exchange(c)
    h = auth(issued)
    seen = []

    async def respond(body, secret):
        seen.append(body)
        data = {"model": "upstream", "choices": [{"message": {"content": "FAKE"}}]}
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode())
        return httpx.Response(200, json=data)

    provider.chat = respond
    assert (await c.get("/v1/models")).status_code == 401
    assert [m["id"] for m in (await c.get("/v1/models", headers=h)).json()["data"]] == ids
    for model in ids:
        body = {"model": model, "messages": [{"role": "user", "content": "Synthetic"}], "max_tokens": 16}
        r = await c.post("/v1/chat/completions", headers=h, json=body)
        assert r.status_code == 200 and r.json()["model"] == model
        r = await c.post("/v1/chat/completions", headers=h, json={**body, "stream": True})
        assert r.status_code == 200 and f'"model":"{model}"' in r.text and "[DONE]" in r.text
    assert {b["model"] for b in seen} == {"fake/tool-coder"}
    async with app.state.db.tx() as db:
        await run(db, "UPDATE policies SET aliases=:a WHERE id='default'", a=ids[:1])
    assert (await c.get("/me", headers=h)).json()["permissions"]["models"] == ids[:1]
    assert [m["id"] for m in (await c.get("/v1/models", headers=h)).json()["data"]] == ids[:1]
    r = await c.post("/v1/chat/completions", headers=h,
        json={"model": ids[1], "messages": [{"role": "user", "content": "Synthetic"}]})
    assert r.status_code == 403 and len(seen) == 4


async def test_provider_grant_is_checked_before_paid_key_creation(stack):
    app, c, _, _, provider = stack
    async with app.state.db.tx() as conn:
        await run(conn, "UPDATE policies SET providers=ARRAY[]::text[] WHERE id='default'")
    response = await exchange(c)
    assert response.status_code == 403 and response.json()["error"]["code"] == "provider_not_allowed"
    assert provider.created == 0
    async with app.state.db.tx() as conn:
        principal = await one(conn, "SELECT provider_allocated FROM principals")
        count = await one(conn, "SELECT count(*) AS n FROM provisioning_operations")
    assert principal["provider_allocated"] == 0 and count["n"] == 0


async def test_uncertain_no_match_never_recreates(stack):
    app, c, _, _, p = stack
    owner = uid("subject_")
    p.lost = True
    assert (await exchange(c, owner)).status_code == 503
    p.keys.clear()  # An empty list is insufficient proof that an in-flight create cannot still appear.
    async with app.state.db.tx() as db:
        await run(db, "UPDATE provisioning_operations SET updated_at=now()-interval '31 seconds'")
    await app.state.broker.tick()
    p.lost = False
    assert (await exchange(c, owner)).status_code == 503 and p.created == 1


async def test_no_secret_in_success_error_retry_audit(stack, caplog):
    app, c, _, _, p = stack
    r = await exchange(c)
    h, owner = auth(r), r.json()["principal"]["id"]
    assert (await c.post("/v1/chat/completions", headers=h,
        json={"model":"company-coder","messages":[{"role":"user","content":"PRIVATE_PROMPT"}],"provider":"EVIL"})).status_code == 400
    p.revoke_fails = True
    await app.state.auth.suspend(owner, "safe-correlation")
    await app.state.broker.tick()
    async with app.state.db.tx() as db:
        records = await rows(db, "SELECT * FROM audit_events WHERE actor=:p OR correlation='safe-correlation'", p=owner)
    output = json.dumps([dict(x) for x in records], default=str) + caplog.text
    assert "UPSTREAM_SECRET" not in output and "PRIVATE_PROMPT" not in output and r.json()["credential"]["key"] not in output


@pytest.mark.parametrize("credential_mode", ["static", "none"])
async def test_generic_shared_upstream_sso_json_sse_and_revoke(stack, monkeypatch, credential_mode):
    from gateway.catalog import read_catalog
    from providers.factory import providers

    first, _, original, identity, _ = stack
    s = Settings(_env_file=None, **{**original.model_dump(), "provider_mode": "openai-compatible",
        "provider_credential_mode": credential_mode, "provider_inference_base_url": "https://inference.example/v1",
        "provider_api_key": "SHARED_FIXTURE" if credential_mode == "static" else "",
        "provider_management_base_url": "", "provider_catalog_base_url": "", "model_catalog": read_catalog("builtin:y-ai")})
    ids = [m.id for m in s.models]
    async with first.state.db.tx() as conn:
        await run(conn, "UPDATE policies SET providers=:p,aliases=:a WHERE id='default'", p=[s.provider_mode], a=ids)
    seen = []

    async def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers.get("authorization") == ("Bearer SHARED_FIXTURE" if credential_mode == "static" else None)
        body = json.loads(request.content)
        seen.append(body)
        obj = {"model": "upstream", "choices": [{"index": 0, "message": {"content": "OK"}, "delta": {"content": "OK"}}],
               "usage": {"prompt_tokens": 4, "completion_tokens": 1}}
        if body["stream"]:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=("data: " + json.dumps(obj) + "\n\ndata: [DONE]\n\n").encode())
        return httpx.Response(200, json=obj)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        monkeypatch.setattr("gateway.app.providers", lambda selected, _: providers(selected, upstream))
        app = create_app(s, identity=identity, worker=False)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=s.gateway_public_url) as client:
            issued = await exchange(client)
            headers = auth(issued)
            assert "SHARED_FIXTURE" not in issued.text
            assert (await client.get("/health/ready")).status_code == 200
            assert [m["id"] for m in (await client.get("/v1/models", headers=headers)).json()["data"]] == ids
            for model in ids:
                for stream in (False, True):
                    response = await client.post("/v1/chat/completions", headers=headers,
                        json={"model": model, "messages": [{"role": "user", "content": "Synthetic"}],
                              "stream": stream, "max_tokens": 16})
                    assert response.status_code == 200 and "SHARED_FIXTURE" not in response.text
                    assert ("[DONE]" in response.text and f'"model":"{model}"' in response.text) if stream else response.json()["model"] == model
            owner = issued.json()["principal"]["id"]
            async with app.state.db.tx() as conn:
                assert (await one(conn, "SELECT count(*) AS n FROM provider_credentials"))["n"] == 0
                assert (await one(conn, "SELECT count(*) AS n FROM provisioning_operations"))["n"] == 0
                assert float((await one(conn, "SELECT provider_allocated FROM principals WHERE id=:p", p=owner))["provider_allocated"]) == 0
                assert (await one(conn, "SELECT count(*) AS n FROM usage_records WHERE outcome='success'"))["n"] == 4
                await run(conn, "UPDATE policies SET providers=ARRAY[]::text[] WHERE id='default'")
            assert (await exchange(client)).status_code == 403
            assert (await client.post("/v1/chat/completions", headers=headers,
                json={"model": ids[0], "messages": [{"role": "user", "content": "Denied"}]})).status_code == 403
            assert len(seen) == 4
            assert (await client.delete("/auth/cli/credentials/current", headers=headers)).status_code == 204
            assert (await client.get("/me", headers=headers)).status_code == 401


async def test_managed_migration_reuses_existing_encrypted_key_and_allocation(stack):
    first, _, original, identity, provider = stack
    old = original.model_copy(update={"provider_mode": "openrouter-managed"})
    async with first.state.db.tx() as conn:
        await run(conn, "UPDATE policies SET providers=ARRAY['openrouter-managed'] WHERE id='default'")
    owner = uid("subject_")
    app = create_app(old, identity, provider, worker=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=old.gateway_public_url) as client:
        issued = await exchange(client, owner)
        headers = auth(issued)
        principal = issued.json()["principal"]["id"]
        previous = await app.state.broker.active(principal)
        previous_secret = app.state.broker.secret(previous)
    # Only the permission grant changes; AAD/provider IDs and all budgets remain intact.
    async with first.state.db.tx() as conn:
        await run(conn, "UPDATE policies SET providers=ARRAY['openai-compatible'] WHERE id='default'")
    generic = old.model_copy(update={"provider_mode": "openai-compatible", "provider_credential_mode": "openrouter-managed"})
    app = create_app(generic, identity, provider, worker=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=generic.gateway_public_url) as client:
        assert (await client.get("/me", headers=headers)).status_code == 200
        assert (await exchange(client, owner)).status_code == 200
        current = await app.state.broker.active(principal)
        assert current["id"] == previous["id"] and provider.created == 1
        assert await app.state.broker.inference_secret(principal) == previous_secret
        async with app.state.db.tx() as conn:
            assert float((await one(conn, "SELECT provider_allocated FROM principals WHERE id=:p", p=principal))["provider_allocated"]) == old.openrouter_test_key_limit_usd
        await app.state.broker.ensure(principal, rotate=True)
        await app.state.broker.tick()
        assert previous["provider_reference"] in provider.revoked


async def test_usage_counters_correct_only_positive_observed_delta_and_not_twice(stack):
    app, client, _, _, _ = stack
    issued = await exchange(client)
    owner, credential = issued.json()["principal"]["id"], issued.json()["credential"]["id"]
    rid = uid("req_")
    await app.state.inference.reserve(owner, credential, "company-coder", 100, rid)
    usage = Usage()
    usage.prompt, usage.completion = 100, 50
    await app.state.inference.finish(rid, usage, "success")
    await app.state.inference.finish(rid, usage, "success")
    async with app.state.db.tx() as conn:
        daily = await one(conn, "SELECT charged_units FROM usage_daily WHERE principal_id=:p", p=owner)
        principal = await one(conn, "SELECT usage_charged_units,usage_requests FROM principals WHERE id=:p", p=owner)
    assert daily["charged_units"] == principal["usage_charged_units"] == 150 and principal["usage_requests"] == 1
    assert (await client.get("/me", headers=auth(issued))).json()["usage"] == {"charged_units": 150, "requests": 1}
    async with app.state.db.tx() as conn:
        await run(conn, "DELETE FROM usage_records WHERE id=:id", id=rid)
    # Archiving/deleting old detail records must never restore lifetime quota.
    assert (await client.get("/me", headers=auth(issued))).json()["usage"] == {"charged_units": 150, "requests": 1}


async def test_concurrent_stale_authentication_refreshes_identity_once(stack):
    app, client, _, identity, _ = stack
    issued = await exchange(client)
    async with app.state.db.tx() as conn:
        await run(conn, "UPDATE principals SET authz_checked_at=now()-interval '61 seconds'")
    calls = 0
    async def status(subject):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.05)
        return True
    identity.status = status
    responses = await asyncio.gather(*(client.get("/me", headers=auth(issued)) for _ in range(30)))
    assert all(response.status_code == 200 for response in responses) and calls == 1


async def test_migration_backfills_old_usage_and_accounts_for_old_writer(stack):
    app, _, _, _, _ = stack
    schema = "migration_" + uuid4().hex
    async with app.state.db.tx() as conn:
        await run(conn, f'CREATE SCHEMA "{schema}"')
    db = DB(app.state.db.engine.url.render_as_string(hide_password=False), schema)
    try:
        async with db.tx() as conn:
            migrations = Path(__file__).resolve().parents[1] / "migrations"
            for statement in (migrations / "schema.sql").read_text().split(";"):
                if statement.strip():
                    await run(conn, statement)
            await run(conn, "INSERT INTO policies VALUES ('p',ARRAY['model'],ARRAY['fake'],30,3,10000,10000,1)")
            await run(conn, "INSERT INTO principals(id,tenant,issuer,subject,policy_id) VALUES ('u','t','i','s','p')")
            await run(conn, """INSERT INTO usage_records(id,principal_id,model,reserved_units,charged_units,created_at,lease_expires_at)
                VALUES ('old','u','model',100,100,now()-interval '2 days',now()-interval '1 day')""")
            await run(conn, (migrations / "usage_counters.sql").read_text())
            await run(conn, """INSERT INTO usage_records(id,principal_id,model,reserved_units,charged_units,lease_expires_at)
                VALUES ('new','u','model',50,50,now()+interval '1 minute')""")
            await run(conn, "UPDATE usage_records SET charged_units=125 WHERE id='old'")
            p = await one(conn, "SELECT usage_charged_units,usage_requests FROM principals WHERE id='u'")
            daily = await rows(conn, "SELECT charged_units FROM usage_daily ORDER BY day")
            assert p["usage_charged_units"] == 175 and p["usage_requests"] == 2
            assert [r["charged_units"] for r in daily] == [125, 50]
    finally:
        await db.engine.dispose()
        async with app.state.db.tx() as conn:
            await run(conn, f'DROP SCHEMA "{schema}" CASCADE')


async def test_two_api_replicas_enforce_one_concurrency_quota_and_immediate_device_revoke(stack):
    app, client, settings, identity, provider = stack
    issued = await exchange(client)
    owner, credential = issued.json()["principal"]["id"], issued.json()["credential"]["id"]
    replica = create_app(settings, identity, provider, worker=False)
    async with replica.router.lifespan_context(replica), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replica), base_url=settings.gateway_public_url) as other:
        results = await asyncio.gather(*((app if index%2 else replica).state.inference.reserve(
            owner, credential, "company-coder", 100, uid("req_")) for index in range(12)), return_exceptions=True)
        assert sum(result is None for result in results) == 3
        assert all(isinstance(result, GatewayError) and result.status == 429 for result in results if result is not None)
        async with app.state.db.tx() as conn:
            counter = await one(conn, "SELECT usage_charged_units FROM principals WHERE id=:p", p=owner)
        assert counter["usage_charged_units"] == 300
        assert (await other.get("/me", headers=auth(issued))).status_code == 200
        assert (await client.delete("/auth/cli/credentials/current", headers=auth(issued))).status_code == 204
        assert (await other.get("/me", headers=auth(issued))).status_code == 401
