"""Network integration: actual Keycloak, gateway container, PostgreSQL, FAKE provider.

All users are created for this test and deleted afterwards. No external model is contacted.
"""
import asyncio
import os
import time
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv

from gateway.config import Settings
from gateway.db import DB, one
from tools.device_login import access_token
from tools.keycloak_test_user import test_user as local_user


@pytest.fixture
def dev():
    load_dotenv()
    s = Settings()
    if s.profile != "dev" or s.provider_mode != "fake":
        pytest.skip("Requires project dev compose and bootstrap")
    return s


async def login(c, s, username, password):
    token = await access_token(s.oidc_issuer, username, password)
    r = await c.post(s.gateway_public_url + "/auth/cli/exchange",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": uuid4().hex},
        json={"device_name": "real-network-test", "client": "kilo-cli"})
    return r


async def control(c, s, body=None):
    url = s.fake_provider_url.replace("/v1", "") + "/control"
    h = {"Authorization": "Bearer " + os.environ["FAKE_MANAGEMENT_KEY"]}
    return (await c.request("POST" if body is not None else "GET", url, headers=h, json=body)).json()


def chat(stream=False, tools=False, model=None):
    body = {"model": model or Settings().models[0].id, "messages": [{"role": "user", "content": "Local synthetic test"}],
            "max_tokens": 64, "stream": stream}
    if tools:
        body.update(tools=[{"type":"function","function":{"name":"echo","parameters":{"type":"object","properties":{"value":{"type":"string"}}}}}], tool_choice="required")
    return body


async def test_actual_device_roles_and_model_roundtrip(dev):
    s = dev
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
        before = (await control(c, s))["created"]
        async with local_user(False) as (name, password, _, _):
            r = await login(c, s, name, password)
            assert r.status_code == 403
        assert (await control(c, s))["created"] == before
        async with local_user() as (name, password, _, _):
            r = await login(c, s, name, password)
            assert r.status_code == 200, r.status_code
            key, owner = r.json()["credential"]["key"], r.json()["principal"]["id"]
            h = {"Authorization": "Bearer " + key}
            catalog = (await c.get(s.gateway_public_url+"/v1/models", headers=h)).json()
            assert [m["id"] for m in catalog["data"]] == [m.id for m in s.models]
            assert (await c.get(s.gateway_public_url+"/me", headers=h)).json()["permissions"]["models"] == [m.id for m in s.models]
            for model in s.models:
                result = await c.post(s.gateway_public_url+"/v1/chat/completions", headers=h, json=chat(model=model.id))
                assert result.status_code == 200 and result.json()["model"] == model.id
                assert "FAKE" in result.json()["choices"][0]["message"]["content"]
            r = await c.post(s.gateway_public_url+"/v1/chat/completions", headers=h, json=chat(tools=True))
            assert r.status_code == 200
            message = r.json()["choices"][0]["message"]
            call = message["tool_calls"][0]
            body = chat()
            body["messages"].extend([message, {"role":"tool","tool_call_id":call["id"],"content":"FAKE tool result"}])
            r = await c.post(s.gateway_public_url+"/v1/chat/completions", headers=h, json=body)
            assert r.status_code == 200 and "FAKE" in r.json()["choices"][0]["message"]["content"]
            db = DB(s.database_url.get_secret_value())
            async with db.tx() as q:
                result = await one(q, "SELECT count(*) AS n FROM usage_records WHERE principal_id=:p AND outcome='success'", p=owner)
                assert result["n"] == 2 + len(s.models)
            await db.engine.dispose()
            assert (await c.delete(s.gateway_public_url+"/auth/cli/credentials/current", headers=h)).status_code == 204
            assert (await c.get(s.gateway_public_url+"/me", headers=h)).status_code == 401


async def test_actual_stream_latency_tool_arguments_disconnect_and_errors(dev):
    s = dev
    async with local_user() as (name, password, _, _), httpx.AsyncClient(trust_env=False, timeout=30) as c:
        r = await login(c, s, name, password)
        assert r.status_code == 200
        h = {"Authorization":"Bearer " + r.json()["credential"]["key"]}
        url = s.gateway_public_url + "/v1/chat/completions"
        try:
            await control(c, s, {"delay": 0.3})
            start, first, output = time.monotonic(), None, ""
            async with c.stream("POST", url, headers=h, json=chat(stream=True, tools=True)) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        first = first or time.monotonic()
                    output += line
            elapsed = time.monotonic()-start
            assert first-start < elapsed-0.4  # First chunk arrives well before completion.
            assert '"tool_calls"' in output and '"arguments"' in output and "[DONE]" in output
            before = (await control(c, s))["cancelled"]
            async with c.stream("POST", url, headers=h, json=chat(stream=True)) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        break
            await asyncio.sleep(0.3)
            assert (await control(c, s))["cancelled"] > before
            for upstream, expected, status in ((401,"provider_key_rejected",502), (402,"provider_insufficient_balance",502),
                                               (429,"provider_rate_limited",429), (503,"provider_unavailable",502)):
                await control(c, s, {"chat_status": upstream})
                r = await c.post(url, headers=h, json=chat())
                assert r.status_code == status and r.json()["error"]["code"] == expected
                if status == 429:
                    assert int(r.headers["retry-after"]) > 0
            await control(c, s, {"chat_status":200,"midstream":True})
            r = await c.post(url, headers=h, json=chat(stream=True))
            assert r.status_code == 200 and "provider_stream_error" in r.text
        finally:
            await control(c, s, {"delay":0.05,"midstream":False,"chat_status":200})


async def test_actual_keycloak_disable_within_freshness(dev):
    s = dev
    async with local_user() as (name, password, ident, admin), httpx.AsyncClient(trust_env=False,timeout=30) as c:
        r = await login(c, s, name, password)
        assert r.status_code == 200
        h = {"Authorization":"Bearer " + r.json()["credential"]["key"]}
        await admin("PUT", "/users/"+ident, {"enabled":False})
        await asyncio.sleep(s.authz_max_staleness_seconds + 0.2)
        r = await c.get(s.gateway_public_url+"/me", headers=h)
        assert r.status_code in {401,403}
