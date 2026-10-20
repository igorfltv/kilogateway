import json

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.catalog import validate_upstream_catalog
from gateway.errors import GatewayError
from gateway.profile import public_profile
from providers.factory import providers
from providers.openai_compatible import OpenAICompatibleAdapter


def generic(settings, **updates):
    values = {**settings.model_dump(), "provider_mode": "openai-compatible", "model_catalog_file": "builtin:y-ai",
              "provider_inference_base_url": "https://inference.example/private/v1", "provider_api_key": "INFERENCE_FIXTURE",
              **updates}
    return Settings(_env_file=None, **values)


def test_default_mode_is_generic_and_production_requires_explicit_upstream(settings, tmp_path):
    values = settings.model_dump(exclude={"provider_mode", "model_catalog_file", "model_catalog"})
    values["profile"] = "production"
    values["gateway_public_url"] = "https://gateway.example"
    values["oidc_issuer"] = "https://sso.example/realms/company"
    with pytest.raises(ValidationError, match="base URL and model catalog required"):
        Settings(_env_file=None, **values)
    values["provider_inference_base_url"] = "https://inference.example/v1"
    with pytest.raises(ValidationError, match="server API key required"):
        Settings(_env_file=None, **values)
    s = Settings(_env_file=None, **{**values, "provider_api_key": "INFERENCE_FIXTURE"})
    assert s.provider_mode == "openai-compatible" and s.shared_provider_credential
    assert len(s.models) == 2
    with pytest.raises(ValidationError):
        generic(settings, provider_credential_mode="none")
    with pytest.raises(ValidationError):
        generic(settings, provider_credential_mode="openrouter-managed", openrouter_management_key="MANAGEMENT_FIXTURE")
    for name in ("PROVIDER_API_KEY", "PROVIDER_CATALOG_API_KEY"):
        path = tmp_path / "unsafe.env"
        path.write_text(name + "=FIXTURE\n")
        with pytest.raises(ValueError, match="unsupported or secret"):
            public_profile(path)


def test_standard_catalog_ids_and_explicit_extended_metadata_validation(settings):
    s = generic(settings)
    standard = [{"id": m.id, "object": "model", "owned_by": "company"} for m in s.models]
    validate_upstream_catalog(s, standard)
    with pytest.raises(GatewayError, match="configured_model_missing"):
        validate_upstream_catalog(s, standard[:1])
    s.provider_catalog_validation = "metadata"
    with pytest.raises(GatewayError, match="configured_context_exceeds_provider"):
        validate_upstream_catalog(s, standard)
    extended = [{**m, "context_length": 262144, "top_provider": {"max_completion_tokens": 4096},
                 "supported_parameters": ["tools", "tool_choice"]} for m in standard]
    validate_upstream_catalog(s, extended)


@pytest.mark.parametrize("authenticated", [True, False])
async def test_generic_server_without_any_management_api(settings, authenticated):
    seen = []
    s = generic(settings, provider_credential_mode="static" if authenticated else "none",
                provider_api_key="INFERENCE_FIXTURE" if authenticated else "")

    async def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m.id} for m in s.models]})
        assert request.url.path == "/private/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == s.models[0].id
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": {"content": "OK"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        inference, manager = providers(s, client)
        assert manager is None
        assert [m["id"] for m in await inference.list_models()] == [m.id for m in s.models]
        response = await inference.chat({"model": s.models[0].id}, s.provider_api_key.get_secret_value())
        assert response.json()["choices"][0]["message"]["content"] == "OK"
        await response.aclose()
    authorization = "Bearer INFERENCE_FIXTURE" if authenticated else None
    assert seen == [("GET", "/private/v1/models", authorization), ("POST", "/private/v1/chat/completions", authorization)]


async def test_separate_catalog_never_receives_inference_or_management_key(settings):
    seen = []

    async def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "test"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for catalog_key in ("", "CATALOG_FIXTURE"):
            s = generic(settings, provider_catalog_base_url="https://catalog.example/list/v1",
                        provider_catalog_api_key=catalog_key, openrouter_management_key="MANAGEMENT_FIXTURE")
            inference, _ = providers(s, client)
            await inference.list_models()
    assert seen == [None, "Bearer CATALOG_FIXTURE"]


async def test_managed_generic_transport_preserves_credential_namespace(settings):
    s = generic(settings, provider_api_key="", provider_credential_mode="openrouter-managed",
                provider_management_base_url="https://management.example/admin/v1",
                openrouter_management_key="MANAGEMENT_FIXTURE")
    seen = []

    async def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "management.example":
            return httpx.Response(201, json={"data": {"hash": "ref"}, "key": "CHILD_FIXTURE"})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        inference, manager = providers(s, client)
        assert type(inference) is OpenAICompatibleAdapter and s.credential_provider == "openrouter-managed"
        child = await manager.provision("label", 0.25, "2026-10-20T23:00:00Z")
        response = await inference.chat({"model": "test"}, child.secret)
        await response.aclose()
        await inference.list_models()
    assert seen == [("management.example", "Bearer MANAGEMENT_FIXTURE"),
                    ("inference.example", "Bearer CHILD_FIXTURE"), ("inference.example", None)]


@pytest.mark.parametrize("method", ["chat", "list_models"])
async def test_generic_rejects_redirects_even_when_client_follows_them(method):
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://capture.invalid/v1/chat/completions"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        adapter = OpenAICompatibleAdapter(client, "https://inference.example/v1", catalog_key="CATALOG_FIXTURE")
        with pytest.raises(GatewayError):
            await (adapter.chat({"model": "test"}, "INFERENCE_FIXTURE") if method == "chat" else adapter.list_models())
    assert seen == ["inference.example"]
