"""Opt-in real OpenRouter lifecycle through an isolated gateway and actual Kilo CLI.

Only synthetic requests are sent. Deletes only this run's keys/users/schema.
The user's regular Kilo configuration, credentials and FAKE stack are preserved.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from gateway.config import Settings
from gateway.schema import initialize_test_schema
from gateway.db import DB, one, rows, run
from gateway.errors import GatewayError
from providers.openrouter import OpenRouterAdapter
from tools.device_login import access_token, approve
from tools.keycloak_test_user import test_user
from tools.kilo_config import build_config
from tools.mcp_login import complete_setup

ROOT = Path(__file__).resolve().parents[1]


def regular_state():
    paths = [Path.home()/".config/kilo/kilo.jsonc", Path.home()/".local/share/kilo/auth.json"]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}


def client_environment(work):
    env = {k:v for k,v in os.environ.items() if k in {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
        "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "SYSTEMROOT"}}
    for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
        p = work / kind.lower()
        p.mkdir()
        env[f"XDG_{kind}_HOME"] = str(p)
    env.update(KILO_DISABLE_DEFAULT_PLUGINS="true", KILO_DISABLE_MODELS_FETCH="true")
    return env


async def cli(binary, args, cwd, env, timeout=180):
    p = await asyncio.create_subprocess_exec(binary, *args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(p.communicate(), timeout)
        if p.returncode or len(output) > 2000000:
            raise RuntimeError("native_cli_failed")
        return output
    finally:
        if p.returncode is None:
            p.terminate()
            await p.wait()


async def native_login(binary, project, env, issuer, username, password):
    p = await asyncio.create_subprocess_exec(binary, "auth", "login", "--provider", "y.ai",
        cwd=project, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, approved, mcp_complete = bytearray(), False, False
    try:
        async with asyncio.timeout(180):
            while chunk := await p.stdout.read(1024):
                output.extend(chunk)
                if len(output) > 200000:
                    raise RuntimeError("native_login_output_limit")
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output.decode(errors="replace"))
                pattern = re.escape(issuer.split("/realms/")[0]) + r"/[^\s]+user_code=[A-Za-z0-9-]+"
                match = re.search(pattern, clean)
                if match and not approved:
                    await approve(match.group(0), username, password)
                    approved = True
                setup = re.search(r"http://(?:localhost|127\.0\.0\.1):[0-9]+/[^\s]*auth/mcp/setup#ms_[\w-]+\.[\w-]+", clean)
                if setup and not mcp_complete:
                    await complete_setup(setup.group(0))
                    mcp_complete = True
            if await p.wait() or not approved:
                clean = re.sub(r"(?:sk-or-v1-|cg_gc_|ms_)[A-Za-z0-9._-]+", "[REDACTED]", output.decode(errors="replace"))
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", clean)
                (project.parent/"native-login.redacted.log").write_text(clean)
                raise RuntimeError("native_login_failed")
    finally:
        if p.returncode is None:
            p.terminate()
            await p.wait()


async def main():
    if os.environ.get("ENABLE_LIVE_OPENROUTER_TESTS") != "1":
        print("SKIPPED: set ENABLE_LIVE_OPENROUTER_TESTS=1 for explicit live validation")
        return
    baseline = regular_state()
    schema = "live_" + uuid4().hex
    work = Path(tempfile.mkdtemp(prefix="live-validation-", dir=ROOT/".local"))
    work.chmod(0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint_file = Path(os.environ.get("YAI_ENDPOINTS_FILE", ROOT/"config/endpoints/openrouter.env"))
    secret_file = Path(os.environ.get("YAI_EXTRA_SECRETS_FILE", ROOT/".local/secrets/openrouter.env"))
    s = Settings(_env_file=(ROOT/".env", secret_file, endpoint_file), profile="dev",
        provider_mode="openrouter-managed", database_schema=schema, oidc_internal_origin="",
        gateway_public_url=f"http://127.0.0.1:{port}", provider_key_ttl_seconds=600,
        openrouter_test_key_limit_usd=0.10, principal_provider_budget_usd=0.25, worker_interval_seconds=0.2)
    local = dotenv_values(ROOT/".env")
    for key in ("KC_ADMIN_USER", "KC_ADMIN_PASSWORD"):
        os.environ[key] = local[key]
    os.environ.update(PROFILE="dev", OIDC_ISSUER=s.oidc_issuer)
    db = DB(s.database_url.get_secret_value(), schema)
    setup = DB(s.database_url.get_secret_value())
    server, server_log = None, None
    refs, labels = set(), set()
    report = {"provider": "openrouter-managed", "models": [m.id for m in s.models], "checks": {},
              "key_limit_usd": 0.10, "planned_generations": 2, "status": "RUNNING"}
    stage = "setup"

    def passed(name):
        report["checks"][name] = "PASS"
        print("PASS: " + name, flush=True)

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=150,
            verify=ssl.create_default_context(cafile=s.tls_ca_bundle) if s.tls_ca_bundle else True) as c:
        provider = OpenRouterAdapter(c, s.openrouter_base_url, s.openrouter_management_key.get_secret_value(), **s.provider_options)

        async def inventory():
            async with db.tx() as conn:
                records = await rows(conn, "SELECT provider_reference,label FROM provider_credentials")
                ops = await rows(conn, "SELECT label FROM provisioning_operations")
            refs.update(r["provider_reference"] for r in records)
            labels.update(r["label"] for r in ops)
            return records

        async def deleted(reference):
            # Never infer deletion from an exception or from DELETE success alone.
            r = await c.get(provider.management_base+provider.keys_path+"/"+reference,
                headers={"Authorization": "Bearer "+s.openrouter_management_key.get_secret_value()})
            return r.status_code == 404

        async def json_chat(key, model, extra=None):
            body = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly OK."}], "max_tokens":4096}
            body.update(extra or {})
            r = await c.post(s.gateway_public_url+"/v1/chat/completions", headers={"Authorization":"Bearer "+key}, json=body)
            if r.status_code != 200:
                code = r.json().get("error",{}).get("code", "unknown")
                raise RuntimeError("gateway_chat_status_"+str(r.status_code)+"_"+re.sub(r"[^a-z0-9_]","",str(code)))
            data = r.json()
            if data.get("model") != model or not data.get("choices") or data.get("error"):
                raise RuntimeError("invalid_chat_response")
            return data

        try:
            async with setup.tx() as conn:
                await run(conn, f'CREATE SCHEMA "{schema}"')
            async with db.tx() as conn:
                await initialize_test_schema(conn, ROOT / "gateway/migrations")
            server_env = {**os.environ, "PYTHONPATH":str(ROOT/"gateway/src")+os.pathsep+str(ROOT),
                "YAI_SECRETS_FILE":str(ROOT/".env"), "YAI_EXTRA_SECRETS_FILE":str(secret_file),
                "YAI_ENDPOINTS_FILE":str(endpoint_file), "DATABASE_SCHEMA":schema,
                "GATEWAY_PUBLIC_URL":s.gateway_public_url, "PROVIDER_MODE":"openrouter-managed",
                "OIDC_INTERNAL_ORIGIN":"", "PROVIDER_KEY_TTL_SECONDS":"600", "WORKER_INTERVAL_SECONDS":"0.2",
                "OPENROUTER_TEST_KEY_LIMIT_USD":"0.10", "PRINCIPAL_PROVIDER_BUDGET_USD":"0.25"}
            server_env.pop("OPENROUTER_MANAGEMENT_KEY", None)
            server_log = (work/"server.log").open("wb")
            server = await asyncio.create_subprocess_exec(sys.executable, "-m", "uvicorn", "gateway.app:create_app",
                "--factory", "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "error",
                cwd=ROOT, env=server_env, stdout=server_log, stderr=server_log)
            for _ in range(100):
                try:
                    if (await c.get(s.gateway_public_url+"/health/ready")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if server.returncode is not None:
                    raise RuntimeError("isolated_gateway_start_failed")
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("isolated_gateway_not_ready")
            passed("isolated_gateway_ready")
            stage = "catalog"
            public = {m["id"]:m for m in await provider.list_models()}
            assert all(m.upstream_id in public for m in s.models)
            passed("public_model_catalog")
            stage = "unauthorized_identity"
            async with test_user(False) as (name, password, _, _):
                token = await access_token(s.oidc_issuer, name, password)
                r = await c.post(s.gateway_public_url+"/auth/cli/exchange", headers={"Authorization":"Bearer "+token,
                    "Idempotency-Key":uuid4().hex}, json={"device_name":"live-denied","client":"kilo-cli"})
                assert r.status_code == 403
            passed("identity_without_roles_denied")
            async with test_user() as (name, password, subject, kc_admin):
                stage = "native_login"
                env = client_environment(work)
                project = work/"project"
                project.mkdir()
                config = build_config(s)
                config["plugin"][0][1]["openBrowser"] = False
                config_path = work/"config/kilo/kilo.json"
                config_path.parent.mkdir()
                config_path.write_text(json.dumps(config))
                binary = shutil.which("kilo") or str(ROOT/"node_modules/.bin/kilo")
                report["kilo_version"] = (await cli(binary,["--version"],project,env)).decode().strip()
                await native_login(binary, project, env, s.oidc_issuer, name, password)
                authfile = work/"data/kilo/auth.json"
                auth = json.loads(authfile.read_text())["y.ai"]
                assert set(auth) == {"type","key","metadata"} and auth["type"] == "api"
                assert auth["metadata"] == {"company_gateway":s.gateway_public_url,"company_issuer":s.oidc_issuer}
                key = auth["key"]
                assert s.openrouter_management_key.get_secret_value() not in authfile.read_text()
                h = {"Authorization":"Bearer "+key}
                me = (await c.get(s.gateway_public_url+"/me",headers=h)).json()
                owner = me["principal"]["id"]
                passed("native_device_login_and_gateway_only_storage")
                records = await inventory()
                assert len(records) == 1
                first_ref = records[0]["provider_reference"]
                await provider.inspect(first_ref)
                passed("real_user_key_provisioned_and_inspected")
                stage = "native_models"
                expected = {"y.ai/"+m.id for m in s.models}
                listed = (await cli(binary,["models","y.ai"],project,env)).decode().splitlines()
                assert {x.strip() for x in listed if x.startswith("y.ai/")} == expected
                catalog = (await c.get(s.gateway_public_url+"/v1/models",headers=h)).json()["data"]
                assert {m["id"] for m in catalog} == {m.id for m in s.models}
                passed("native_and_authenticated_model_catalogs")
                for model in s.models:
                    stage = "model:"+model.id
                    await json_chat(key, model.id)
                    passed("json:"+model.id)
                    body = {"model":model.id,"messages":[{"role":"user","content":"Reply with exactly OK."}],
                            "max_tokens":4096,"stream":True}
                    async with c.stream("POST",s.gateway_public_url+"/v1/chat/completions",headers=h,json=body) as r:
                        assert r.status_code == 200
                        frames = []
                        async for line in r.aiter_lines():
                            if line.startswith("data:"):
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    break
                                event = json.loads(data)
                                assert not event.get("error") and event.get("model",model.id) == model.id
                                frames.append(event)
                        else:
                            raise RuntimeError("missing_stream_done")
                        assert len(frames) > 1 and any(f.get("choices") for f in frames)
                    passed("sse:"+model.id)
                    tool = {"type":"function","function":{"name":"echo","parameters":{"type":"object",
                            "properties":{"value":{"type":"string"}},"required":["value"]}}}
                    tool_body = {"messages":[{"role":"user","content":"Call echo with value OK."}],
                                 "tools":[tool],"tool_choice":"required"}
                    data = await json_chat(key,model.id,tool_body)
                    message = data["choices"][0]["message"]
                    report.setdefault("tool_response_fields",{})[model.id] = sorted(message)
                    call = message["tool_calls"][0]
                    json.loads(call["function"]["arguments"])
                    await json_chat(key,model.id,{"messages":[*tool_body["messages"],message,
                        {"role":"tool","tool_call_id":call["id"],"content":"OK"}]})
                    passed("tool_roundtrip:"+model.id)
                    output = await cli(binary,["run","--format","json","--model","y.ai/"+model.id,
                        "Reply with exactly OK. Do not call tools."],project,env)
                    assert b'"type":"text"' in output and b'"type":"error"' not in output and b"FAKE:" not in output
                    assert key.encode() not in output and s.openrouter_management_key.get_secret_value().encode() not in output
                    passed("native_run:"+model.id)
                stage = "rotation"
                async with db.tx() as conn:
                    pc = await one(conn,"SELECT id FROM provider_credentials WHERE principal_id=:p AND state='active'",p=owner)
                r = await c.post(s.gateway_public_url+"/admin/provider-credentials/"+pc["id"]+"/rotate",
                    headers={"Authorization":"Bearer "+s.admin_token.get_secret_value()})
                assert r.status_code == 200
                records = await inventory()
                assert len(records) == 2
                for _ in range(60):
                    if await deleted(first_ref):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("old_provider_key_not_deleted")
                await json_chat(key,s.models[0].id)
                passed("rotation_old_key_deleted_new_key_serves_requests")
                # A second device credential reuses the rotated upstream key.
                token = await access_token(s.oidc_issuer,name,password)
                r = await c.post(s.gateway_public_url+"/auth/cli/exchange", headers={"Authorization":"Bearer "+token,
                    "Idempotency-Key":uuid4().hex},json={"device_name":"live-second-device","client":"kilo-cli"})
                assert r.status_code == 200
                second = r.json()["credential"]["key"]
                assert len(await inventory()) == 2
                stage = "logout_revoke"
                await cli(binary,["auth","logout","y.ai"],project,env,45)
                assert "y.ai" not in json.loads(authfile.read_text())
                assert (await c.get(s.gateway_public_url+"/me",headers=h)).status_code == 200
                assert (await c.delete(s.gateway_public_url+"/auth/cli/credentials/current",headers=h)).status_code == 204
                assert (await c.get(s.gateway_public_url+"/me",headers=h)).status_code == 401
                passed("native_logout_and_server_device_revoke")
                stage = "identity_revocation"
                await kc_admin("PUT","/users/"+subject,{"enabled":False})
                await asyncio.sleep(s.authz_max_staleness_seconds+0.2)
                assert (await c.get(s.gateway_public_url+"/me",headers={"Authorization":"Bearer "+second})).status_code == 403
                for _ in range(60):
                    if all([await deleted(ref) for ref in refs]):
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise RuntimeError("identity_revoke_cleanup_incomplete")
                passed("keycloak_disable_blocks_access_and_deletes_provider_key")
                async with db.tx() as conn:
                    usage = await one(conn,"SELECT count(*) AS requests,sum(provider_cost) AS cost FROM usage_records WHERE principal_id=:p",p=owner)
                    audit = await rows(conn,"SELECT actor,operation,target,outcome,correlation FROM audit_events")
                report["requests"] = usage["requests"]
                report["provider_reported_cost_usd"] = str(usage["cost"]) if usage["cost"] is not None else None
                serialized = json.dumps([dict(x) for x in audit])
                assert key not in serialized and second not in serialized and s.openrouter_management_key.get_secret_value() not in serialized
                passed("usage_and_secret_free_audit")
            report["test_identity_cleanup"] = "PASS"
            report["status"] = "PASS"
        except Exception as error:
            report.update(status="FAIL",failed_stage=stage,error_type=type(error).__name__)
            if isinstance(error, GatewayError):
                report["error_code"] = error.code
            if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z0-9_]+", str(error)):
                report["error_code"] = str(error)
            print("FAIL: "+stage+"; raw errors and secrets suppressed",flush=True)
        finally:
            cleanup = True
            try:
                await inventory()
            except Exception:
                cleanup = False
            for label in labels:
                try:
                    for ref in await provider.find(label):
                        refs.add(ref)
                except Exception:
                    cleanup = False
            for ref in refs:
                try:
                    await provider.revoke(ref)
                    cleanup = await deleted(ref) and cleanup
                except Exception:
                    cleanup = False
            report["provider_key_cleanup"] = "PASS" if cleanup else "FAIL"
            if server and server.returncode is None:
                server.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(server.wait(),15)
                if server.returncode is None:
                    server.kill()
                    await server.wait()
            if server_log:
                server_log.close()
            await db.engine.dispose()
            async with setup.tx() as conn:
                await run(conn,f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            report["isolated_schema_cleanup"] = "PASS"
            await setup.engine.dispose()
            report["regular_kilo_state_preserved"] = regular_state() == baseline
            if not cleanup or not report["regular_kilo_state_preserved"]:
                report["status"] = "FAIL"
            path = ROOT/"artifacts/reports/live-validation.json"
            path.write_text(json.dumps(report,indent=2)+"\n")
            print("Report: artifacts/reports/live-validation.json; status="+report["status"],flush=True)
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
