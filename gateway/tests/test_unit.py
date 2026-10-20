import json

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from gateway.config import trusted_url
from gateway.errors import GatewayError
from gateway.inference import Chat, Inference, Usage, proxy_sse
from gateway.secrets import AESSecretStore, digest
from providers.corporate import CorporateProviderAdapter
from providers.openrouter import OpenRouterAdapter, upstream_error


def test_exchange_accepts_only_supported_cli_clients():
    from gateway.app import ExchangeBody
    for client in ("kilo-cli", "opencode-cli"):
        assert ExchangeBody(device_name="local",client=client).client==client
    with pytest.raises(ValidationError):
        ExchangeBody(device_name="local",client="untrusted-client")


def test_secret_binding_and_unique_nonce(settings):
    store = AESSecretStore(settings.encryption_key.get_secret_value(), "v1")
    a, b = store.seal("SECRET", "owner-a"), store.seal("SECRET", "owner-a")
    assert a != b and "SECRET" not in a and store.open(a, "owner-a") == "SECRET"
    with pytest.raises(InvalidTag):
        store.open(a, "owner-b")
    assert digest("secret", "pepper") != digest("secret", "different")


@pytest.mark.parametrize("url", ["http://evil.test", "https://user:pass@example.com", "https://ok.test?token=bad", "file:///tmp/key"])
def test_urls_rejected(url):
    with pytest.raises(ValueError):
        trusted_url(url, True)


def test_production_fails_closed(settings):
    with pytest.raises(ValidationError):
        type(settings)(**{**settings.model_dump(), "profile": "production"})


async def test_corporate_explicitly_unconfigured():
    p = CorporateProviderAdapter()
    assert not p.capabilities().managed_keys
    for fn in (p.provision, p.chat, p.inspect, p.list_models, p.revoke, p.find):
        with pytest.raises(GatewayError, match="provider_not_configured"):
            await fn()


def test_strict_chat_and_limits(settings):
    inference = Inference(settings, None, None)
    policy = {"aliases": ["company-coder"], "providers": ["fake"]}
    for field in ("provider", "models", "base_url", "route", "transforms", "plugins"):
        with pytest.raises(ValidationError):
            Chat(model="company-coder", messages=[{"role": "user", "content": "hello"}], **{field: "evil"})
    with pytest.raises(GatewayError, match="model_not_allowed"):
        inference.prepare(Chat(model="upstream-direct", messages=[{"role": "user", "content": "x"}]), policy)
    with pytest.raises(GatewayError, match="output_limit"):
        inference.prepare(Chat(model="company-coder", messages=[{"role": "user", "content": "x"}], max_tokens=99999), policy)


def test_opencode_stateless_storage_flag(settings):
    inference=Inference(settings,None,None)
    policy={"aliases":["company-coder"],"providers":["fake"]}
    body={"model":"company-coder","messages":[{"role":"user","content":"OK"}],"store":False}
    payload,_=inference.prepare(Chat(**body),policy)
    assert payload["store"] is False
    with pytest.raises(ValidationError):
        Chat(**{**body,"store":True})


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for x in self.chunks:
            yield x


async def test_sse_tool_calls_incremental():
    obj = {"model": "upstream", "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]}}]}
    raw = ("data: " + json.dumps(obj) + "\r\n\r\ndata: [DONE]\r\n\r\n").encode()
    response = httpx.Response(200, stream=Chunks([raw[:10], raw[10:23], raw[23:]]))
    chunks = [x async for x in proxy_sse(response, Usage(), "local")]
    assert b'"model":"local"' in chunks[0] and b"tool_calls" in chunks[0] and chunks[-1] == b"data: [DONE]\n\n"


@pytest.mark.parametrize("raw,code", [(b'data: {"error":{"message":"SECRET"}}\n\n', "provider_stream_error"),
                                     (b'data: {}\n\n', "provider_stream_incomplete"),
                                     (b'data: invalid\n\n', "provider_sse_invalid")])
async def test_sse_safe_errors(raw, code):
    with pytest.raises(GatewayError, match=code) as e:
        _ = [x async for x in proxy_sse(httpx.Response(200, stream=Chunks([raw])), Usage(), "local")]
    assert "SECRET" not in str(e.value)


async def test_openrouter_contract_separates_credentials():
    seen = []

    async def handler(request):
        seen.append(request)
        if request.method == "POST" and request.url.path.endswith("/keys"):
            b = json.loads(request.content)
            assert b["limit_reset"] is None and b["limit"] == 0.25 and b["expires_at"]
            assert b["expires_at"] == "2026-10-20T23:00:00Z"
            return httpx.Response(201, json={"data": {"hash": "hash"}, "key": "INFERENCE"})
        if request.url.path.endswith("chat/completions"):
            return httpx.Response(200, json={"choices": []})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
        p = OpenRouterAdapter(c, "https://openrouter.ai/api/v1", "MANAGEMENT")
        result = await p.provision("operation-id", 0.25, "2026-10-20T23:00:00.123456+00:00")
        r = await p.chat({"model": "test"}, result.secret)
        await r.aclose()
        await p.revoke(result.reference)
    assert [r.headers["authorization"] for r in seen] == ["Bearer MANAGEMENT", "Bearer INFERENCE", "Bearer MANAGEMENT"]
    assert upstream_error(401).code != "gateway_credential_invalid"
