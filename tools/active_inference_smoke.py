"""Opt-in real inference through the active gateway, retaining the user's login/key.

Uses synthetic prompts and an empty isolated Kilo project. It provisions the selected
provider for the authenticated user, without changing native auth or revoking their key.
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx

from gateway.app import create_app
from gateway.config import Settings
from gateway.db import DB, rows
from gateway.errors import GatewayError
from tools.kilo_config import build_config
from tools.live_validate import cli, client_environment

ROOT = Path(__file__).resolve().parents[1]


async def main():
    if os.environ.get("ENABLE_REAL_INFERENCE_CHECKS") != "1":
        print("SKIPPED: set ENABLE_REAL_INFERENCE_CHECKS=1 to send synthetic inference requests")
        return
    s = Settings(oidc_internal_origin="")
    if s.profile != "dev" or s.credential_provider != "openrouter-managed":
        raise RuntimeError("active_check_requires_explicit_local_openrouter_profile")
    authfile = Path(os.environ.get("COMPANY_KILO_AUTH_FILE", Path.home()/".local/share/kilo/auth.json"))
    auth_before = authfile.read_bytes()
    entry = json.loads(auth_before)["y.ai"]
    assert entry["type"] == "api"
    assert entry["metadata"] == {"company_gateway": s.gateway_public_url, "company_issuer": s.oidc_issuer}
    key = entry["key"]
    assert key.startswith("cg_gc_")
    report = {"status": "RUNNING", "provider": s.provider_mode, "gateway": s.gateway_public_url,
              "auth_scope": "isolated-test-user" if os.environ.get("COMPANY_KILO_AUTH_FILE") else "current-user",
              "synthetic_prompts_only": True, "checks": {}, "models": {}}
    report_file = ROOT/"artifacts/reports"/("openai-compatible-kilo.json" if os.environ.get("COMPANY_KILO_AUTH_FILE") else "active-inference.json")
    work = Path(tempfile.mkdtemp(prefix="active-inference-", dir=ROOT/".local"))
    work.chmod(0o700)
    request_ids = []

    def passed(check):
        report["checks"][check] = "PASS"
        print("PASS: " + check, flush=True)

    try:
        # Use the same broker/auth implementation as the server. HTTP from this host
        # reaches the public issuer; the deployed container uses its internal IdP route.
        app = create_app(s, worker=False)
        async with app.router.lifespan_context(app):
            principal, _, policy = await app.state.auth.authenticate(key)
            assert s.provider_mode in policy["providers"]
            pc = await app.state.broker.ensure(principal["id"])
            assert pc["provider"] == "openrouter-managed" and pc["state"] == "active"
            report["provider_key_limit_usd"] = float(pc["allocation"])
            report["provider_key_expires_at"] = pc["expires_at"].isoformat()
        passed("current_login_and_real_user_key")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=150,
                                     headers={"Authorization": "Bearer " + key}) as client:
            me = await client.get(s.gateway_public_url + "/me")
            assert me.status_code == 200
            assert me.json()["permissions"]["providers"] == [s.provider_mode]
            catalog = await client.get(s.gateway_public_url + "/v1/models")
            assert catalog.status_code == 200
            assert {x["id"] for x in catalog.json()["data"]} == {m.id for m in s.models}
            passed("active_gateway_permissions_and_models")

            async def chat(model, extra=None):
                body = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                        "max_tokens": 4096, **(extra or {})}
                response = await client.post(s.gateway_public_url + "/v1/chat/completions", json=body)
                if response.status_code != 200:
                    code = response.json().get("error", {}).get("code", "unknown")
                    raise RuntimeError("active_chat_" + str(response.status_code) + "_" + code)
                request_ids.append(response.headers["x-request-id"])
                data = response.json()
                assert data["model"] == model and data.get("choices") and not data.get("error")
                return data

            env = client_environment(work)
            conf = build_config(s)
            conf["plugin"][0][1]["openBrowser"] = False
            config_file = work/"config/kilo/kilo.json"
            config_file.parent.mkdir(mode=0o700)
            config_file.write_text(json.dumps(conf))
            isolated_auth = work/"data/kilo/auth.json"
            isolated_auth.parent.mkdir(mode=0o700)
            isolated_auth.write_text(json.dumps({"y.ai": entry}))
            isolated_auth.chmod(0o600)
            project = work/"project"
            project.mkdir()
            binary = shutil.which("kilo") or str(ROOT/"node_modules/.bin/kilo")
            report["kilo_version"] = (await cli(binary, ["--version"], project, env)).decode().strip()
            for model in s.models:
                data = await chat(model.id)
                text = data["choices"][0]["message"].get("content") or ""
                assert text.strip() and "FAKE:" not in text
                passed("json:" + model.id)
                stream_text, done = "", False
                async with client.stream("POST", s.gateway_public_url + "/v1/chat/completions", json={
                        "model": model.id, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                        "max_tokens": 4096, "stream": True}) as response:
                    assert response.status_code == 200
                    request_ids.append(response.headers["x-request-id"])
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        frame = line[5:].strip()
                        if frame == "[DONE]":
                            done = True
                            break
                        event = json.loads(frame)
                        assert not event.get("error")
                        for choice in event.get("choices", []):
                            stream_text += choice.get("delta", {}).get("content") or ""
                assert done and stream_text.strip() and "FAKE:" not in stream_text
                passed("sse:" + model.id)
                tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object",
                    "properties": {"value": {"type": "string"}}, "required": ["value"]}}}]
                prompt = [{"role": "user", "content": "Call echo with value OK."}]
                tool_response = await chat(model.id, {"messages": prompt, "tools": tools, "tool_choice": "required"})
                message = tool_response["choices"][0]["message"]
                call = message["tool_calls"][0]
                assert call["function"]["name"] == "echo"
                assert json.loads(call["function"]["arguments"])["value"] == "OK"
                result = await chat(model.id, {"messages": [*prompt, message,
                    {"role": "tool", "tool_call_id": call["id"], "content": "OK"}]})
                assert result["choices"][0]["message"].get("content")
                passed("tool_roundtrip:" + model.id)
                output = await cli(binary, ["run", "--format", "json", "--model", "y.ai/" + model.id,
                    "Reply with exactly OK. Do not call tools."], project, env)
                assert b'"type":"text"' in output and b'"type":"error"' not in output and b"FAKE:" not in output
                assert key.encode() not in output and s.openrouter_management_key.get_secret_value().encode() not in output
                passed("native_kilo:" + model.id)
                report["models"][model.id] = {"json": "PASS", "sse": "PASS", "tools": "PASS", "native_kilo": "PASS"}
        db = DB(s.database_url.get_secret_value(), s.database_schema)
        try:
            async with db.tx() as conn:
                usage = await rows(conn, """SELECT id,outcome,prompt_tokens,completion_tokens,provider_cost,cost_provenance
                    FROM usage_records WHERE id=ANY(:ids)""", ids=request_ids)
            assert len(usage) == len(request_ids) == 8 and all(x["outcome"] == "success" for x in usage)
            report["direct_http_requests"] = len(usage)
            report["direct_http_reported_cost_usd"] = float(sum(x["provider_cost"] or 0 for x in usage))
            report["direct_http_costs_known"] = all(x["provider_cost"] is not None for x in usage)
            report["native_kilo_runs"] = 2
            passed("direct_http_usage_accounting")
        finally:
            await db.engine.dispose()
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "BLOCKED" if isinstance(error, GatewayError) and error.code == "principal_provider_budget_exhausted" else "FAIL"
        # RuntimeError values above are closed diagnostic codes, never upstream text or credentials.
        report["error"] = error.code if isinstance(error, GatewayError) else (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise
    finally:
        report["native_auth_unchanged"] = hashlib.sha256(authfile.read_bytes()).digest() == hashlib.sha256(auth_before).digest()
        isolated = work/"data/kilo/auth.json"
        isolated.unlink(missing_ok=True)
        if report["status"] == "PASS":
            shutil.rmtree(work)
        report_file.write_text(json.dumps(report, indent=2) + "\n")
        print("Report: " + str(report_file.relative_to(ROOT)) + "; status=" + report["status"], flush=True)
    assert report["native_auth_unchanged"]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Active inference check failed; inspect the sanitized report, secrets suppressed") from None
