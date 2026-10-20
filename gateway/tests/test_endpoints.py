import json
import os
import subprocess
import sys

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.errors import GatewayError
from gateway.oidc import Identity
from providers.openrouter import OpenRouterAdapter
from gateway.profile import public_profile


async def test_separate_management_inference_and_catalog_endpoints():
    seen = []

    async def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.method == "POST" and request.url.path == "/admin/new-keys":
            return httpx.Response(201,json={"key":"INFERENCE_FIXTURE","data":{"hash":"ref"}})
        if request.url.path == "/public/model-list":
            return httpx.Response(200,json={"data":[{"id":"test/model"}]})
        if request.url.path == "/generate/chat":
            return httpx.Response(200,json={"choices":[]})
        return httpx.Response(200,json={"data":{"disabled":False}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False) as c:
        p=OpenRouterAdapter(c,"https://unused.invalid","MANAGEMENT_FIXTURE",
            management_base="https://mgmt.example/admin",inference_base="https://infer.example/generate",
            catalog_base="https://catalog.example/public",keys_path="/new-keys",models_path="/model-list",chat_path="/chat")
        key=await p.provision("label",0.1,"2026-10-20T23:00:00Z")
        await p.inspect(key.reference)
        assert (await p.list_models())[0]["id"] == "test/model"
        r=await p.chat({"model":"test/model"},key.secret)
        await r.aclose()
        await p.revoke(key.reference)
    assert seen == [("https://mgmt.example/admin/new-keys","Bearer MANAGEMENT_FIXTURE"),
        ("https://mgmt.example/admin/new-keys/ref","Bearer MANAGEMENT_FIXTURE"),
        ("https://catalog.example/public/model-list",None),
        ("https://infer.example/generate/chat","Bearer INFERENCE_FIXTURE"),
        ("https://mgmt.example/admin/new-keys/ref","Bearer MANAGEMENT_FIXTURE")]


@pytest.mark.parametrize("updates",[
    {"provider_management_base_url":"http://untrusted.example/api"},
    {"provider_keys_path":"//untrusted.example/keys"},
    {"provider_chat_path":"/a/../chat"},
    {"oidc_token_url":"https://unlisted.example/token"},
])
def test_endpoint_validation_rejects_unsafe_values(settings,updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None,**{**settings.model_dump(),**updates})


async def test_explicit_oidc_discovery_and_trusted_cross_origin_endpoints(settings):
    s=Settings(_env_file=None,**{**settings.model_dump(),
        "oidc_allowed_origins":["https://discovery.example/","https://tokens.example"],
        "oidc_discovery_url":"https://discovery.example/metadata",
        "oidc_token_url":"https://tokens.example/access-token",
        "oidc_jwks_url":"https://tokens.example/keys",
        "oidc_internal_origin":"http://keycloak:8080"})

    async def handler(request):
        assert str(request.url)==s.oidc_discovery_url
        return httpx.Response(200,json={"issuer":s.oidc_issuer,"jwks_uri":"https://untrusted.invalid/jwks",
                                       "token_endpoint":"https://untrusted.invalid/token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        identity=Identity(s,c)
        d=await identity.discover()
        assert d["token_endpoint"]==s.oidc_token_url and d["jwks_uri"]==s.oidc_jwks_url
        assert identity.route(s.oidc_token_url)==s.oidc_token_url
        assert identity.route(s.oidc_issuer+"/device")=="http://keycloak:8080/realms/company/device"
        with pytest.raises(GatewayError,match="oidc_untrusted_endpoint"):
            identity.route("https://untrusted.invalid/token")


def test_endpoint_profile_overrides_addresses_and_preserves_secrets(settings,tmp_path,monkeypatch):
    secret=tmp_path/"secrets.env"
    keys=["database_url","keycloak_status_client_secret","encryption_key","credential_hash_key","admin_token"]
    values=settings.model_dump()
    secret.write_text("PROFILE=dev\nPROVIDER_MODE=fake\n"+"".join(
        k.upper()+"="+(values[k].get_secret_value() if hasattr(values[k],"get_secret_value") else str(values[k]))+"\n" for k in keys))
    endpoint=tmp_path/"endpoints.env"
    endpoint.write_text("GATEWAY_PUBLIC_URL=https://target.example/gateway\nOIDC_ISSUER=https://sso.example/realms/target\n"
                        "PROVIDER_CATALOG_BASE_URL=https://catalog.example/custom\nOIDC_ALLOWED_ORIGINS="+json.dumps([])+"\n")
    monkeypatch.setenv("YAI_SECRETS_FILE",str(secret))
    monkeypatch.setenv("YAI_ENDPOINTS_FILE",str(endpoint))
    s=Settings()
    assert s.gateway_public_url=="https://target.example/gateway"
    assert s.provider_catalog_base_url=="https://catalog.example/custom"
    assert s.admin_token==settings.admin_token and s.encryption_key==settings.encryption_key


def test_container_entrypoint_applies_profile_and_rejects_secrets(tmp_path):
    profile=tmp_path/"endpoints.env"
    profile.write_text("GATEWAY_PUBLIC_URL=https://new.example/gateway\n")
    env={**os.environ,"YAI_ENDPOINTS_FILE":str(profile),"GATEWAY_PUBLIC_URL":"https://legacy.invalid"}
    r=subprocess.run([sys.executable,"-m","gateway",sys.executable,"-c",
        "import os;print(os.environ['GATEWAY_PUBLIC_URL'])"],env=env,capture_output=True,text=True,timeout=10)
    assert r.returncode==0 and r.stdout.strip()=="https://new.example/gateway"
    profile.write_text("OPENROUTER_MANAGEMENT_KEY=NOT_A_REAL_SECRET\n")
    with pytest.raises(ValueError,match="unsupported or secret"):
        public_profile(profile)


def test_persisted_profile_and_secret_selectors_with_environment_override(settings,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("YAI_SECRETS_FILE","YAI_EXTRA_SECRETS_FILE","YAI_ENDPOINTS_FILE"):
        monkeypatch.delenv(key,raising=False)
    keys=["database_url","keycloak_status_client_secret","encryption_key","credential_hash_key","admin_token"]
    values=settings.model_dump()
    (tmp_path/".env").write_text("PROFILE=dev\nPROVIDER_MODE=fake\nYAI_ENDPOINTS_FILE=live.env\n"
        "YAI_EXTRA_SECRETS_FILE=extra.env\n"+"".join(k.upper()+"="+values[k].get_secret_value()+"\n" for k in keys))
    (tmp_path/"extra.env").write_text("OPENROUTER_MANAGEMENT_KEY=TEST_MANAGEMENT_FIXTURE\n")
    (tmp_path/"live.env").write_text("PROVIDER_MODE=openrouter-managed\nMODEL_CATALOG_FILE=builtin:y-ai\n"
                                     "GATEWAY_PUBLIC_URL=http://localhost:18000\n")
    selected=Settings()
    assert selected.provider_mode=="openrouter-managed"
    assert selected.openrouter_management_key.get_secret_value()=="TEST_MANAGEMENT_FIXTURE"
    (tmp_path/"fake.env").write_text("PROVIDER_MODE=fake\nGATEWAY_PUBLIC_URL=http://localhost:18002\n")
    monkeypatch.setenv("YAI_ENDPOINTS_FILE","fake.env")
    override=Settings()
    assert override.provider_mode=="fake" and override.gateway_public_url=="http://localhost:18002"
