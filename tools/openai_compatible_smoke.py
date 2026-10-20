"""Opt-in generic shared-key inference with real SSO and an isolated database schema.

Creates one bounded temporary inference key using the existing management integration.
The tested gateway receives ONLY this static key and never provisions user keys.
"""
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from gateway.app import create_app
from gateway.broker import utcnow
from gateway.catalog import validate_upstream_catalog
from gateway.config import Settings
from gateway.schema import initialize_test_schema
from gateway.db import DB, one, run
from providers.factory import providers
from providers.openrouter import OpenRouterAdapter
from tools.device_login import access_token
from tools.keycloak_test_user import test_user
from tools.live_validate import regular_state

ROOT = Path(__file__).resolve().parents[1]


async def main():
    if os.environ.get("ENABLE_REAL_INFERENCE_CHECKS") != "1":
        print("SKIPPED: set ENABLE_REAL_INFERENCE_CHECKS=1 for real generic upstream checks")
        return
    base = Settings(oidc_internal_origin="")
    assert base.profile == "dev" and base.credential_provider == "openrouter-managed"
    baseline = regular_state()
    report = {"status": "RUNNING", "provider": "openai-compatible", "credential_mode": "static",
              "synthetic_prompts_only": True, "isolated_database_schema": True, "models": {}, "checks": {}}
    schema = "compat_" + uuid4().hex
    setup = DB(base.database_url.get_secret_value())
    schema_created, child = False, None
    label = "company-ai:generic-smoke:" + uuid4().hex

    def passed(name):
        report["checks"][name] = "PASS"
        print("PASS: " + name, flush=True)

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=150) as client:
        manager = OpenRouterAdapter(client, base.openrouter_base_url,
            base.openrouter_management_key.get_secret_value(), **base.provider_options)
        try:
            async with setup.tx() as conn:
                await run(conn, f'CREATE SCHEMA "{schema}"')
            schema_created = True
            report["test_key_label"] = label
            child = await manager.provision(label, 0.25,
                (utcnow() + timedelta(minutes=10)).isoformat())
            s = Settings(_env_file=None, **{**base.model_dump(), "database_schema": schema,
                "provider_mode": "openai-compatible", "provider_credential_mode": "static",
                "provider_api_key": child.secret, "openrouter_management_key": ""})
            inference, credential_manager = providers(s, client)
            assert credential_manager is None
            validate_upstream_catalog(s, await inference.list_models())
            passed("authenticated_generic_catalog")
            app = create_app(s, worker=False)
            async with app.state.db.tx() as conn:
                await initialize_test_schema(conn, ROOT / "gateway/migrations")
            load_dotenv(ROOT / ".env")
            async with test_user() as (username, password, _, _):
                token = await access_token(s.oidc_issuer, username, password)
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=s.gateway_public_url, timeout=150) as gateway:
                    response = await gateway.post("/auth/cli/exchange",
                        headers={"Authorization": "Bearer " + token, "Idempotency-Key": uuid4().hex},
                        json={"device_name": "generic-upstream-smoke", "client": "kilo-cli"})
                    assert response.status_code == 200
                    key = response.json()["credential"]["key"]
                    assert key.startswith("cg_gc_") and child.secret not in response.text
                    headers = {"Authorization": "Bearer " + key}
                    passed("real_device_flow_and_gateway_exchange_without_management_key")
                    response = await gateway.get("/v1/models", headers=headers)
                    assert response.status_code == 200
                    assert {m["id"] for m in response.json()["data"]} == {m.id for m in s.models}
                    for model in s.models:
                        report["current_stage"] = "json:" + model.id
                        body = {"model": model.id, "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                                "max_tokens": 4096}
                        response = await gateway.post("/v1/chat/completions", headers=headers, json=body)
                        report["last_response"] = {"status": response.status_code}
                        if response.status_code != 200:
                            report["last_response"]["error_code"] = response.json().get("error", {}).get("code")
                        assert response.status_code == 200
                        data = response.json()
                        report["last_response"]["finish_reason"] = data.get("choices", [{}])[0].get("finish_reason")
                        report["last_response"]["content_present"] = bool(data.get("choices", [{}])[0].get("message", {}).get("content"))
                        assert data["model"] == model.id and data["choices"][0]["message"].get("content")
                        report["current_stage"] = "sse:" + model.id
                        response = await gateway.post("/v1/chat/completions", headers=headers, json={**body, "stream": True})
                        report["last_response"] = {"status": response.status_code, "done": "[DONE]" in response.text}
                        assert response.status_code == 200 and "[DONE]" in response.text
                        events = [json.loads(line[5:]) for line in response.text.splitlines()
                                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
                        assert all(not event.get("error") for event in events)
                        assert any(choice.get("delta", {}).get("content") for event in events for choice in event.get("choices", []))
                        assert child.secret not in response.text and "FAKE:" not in response.text
                        report["models"][model.id] = {"json": "PASS", "sse": "PASS"}
                        passed("real_static_key_json_and_sse:" + model.id)
                    report.pop("current_stage", None)
                    report.pop("last_response", None)
                    async with app.state.db.tx() as conn:
                        assert (await one(conn, "SELECT count(*) AS n FROM provider_credentials"))["n"] == 0
                        assert (await one(conn, "SELECT count(*) AS n FROM provisioning_operations"))["n"] == 0
                        assert float((await one(conn, "SELECT sum(provider_allocated) AS n FROM principals"))["n"]) == 0
                        usage = await one(conn, """SELECT count(*) AS n,sum(provider_cost) AS cost
                            FROM usage_records WHERE outcome='success'""")
                        assert usage["n"] == 4
                        report["provider_reported_cost_usd"] = float(usage["cost"] or 0)
                    passed("no_per_user_upstream_key_or_allocation")
                    assert (await gateway.delete("/auth/cli/credentials/current", headers=headers)).status_code == 204
                    assert (await gateway.get("/me", headers=headers)).status_code == 401
                    passed("gateway_credential_revoked")
            report["status"] = "PASS"
        except Exception as error:
            report["status"] = "FAIL"
            report["error"] = type(error).__name__
            raise
        finally:
            try:
                refs = [child.reference] if child else await manager.find(label)
                removed = bool(refs)
                for reference in refs:
                    await manager.revoke(reference)
                    response = await client.get(manager.management_base + manager.keys_path + "/" + reference,
                        headers={"Authorization": "Bearer " + base.openrouter_management_key.get_secret_value()})
                    removed = removed and response.status_code == 404
                report["owned_upstream_key_deleted"] = removed
                if not removed:
                    report["status"] = "FAIL"
            except Exception:
                report["status"] = "FAIL"
                report["cleanup_error"] = "upstream_cleanup_failed"
                raise
            finally:
                if schema_created:
                    try:
                        async with setup.tx() as conn:
                            await run(conn, f'DROP SCHEMA "{schema}" CASCADE')
                        report["isolated_schema_removed"] = True
                    except Exception:
                        report["status"] = "FAIL"
                        report["cleanup_error"] = "schema_cleanup_failed"
                await setup.engine.dispose()
                report["regular_kilo_state_preserved"] = regular_state() == baseline
                if not report["regular_kilo_state_preserved"]:
                    report["status"] = "FAIL"
                (ROOT / "artifacts/reports/openai-compatible-static.json").write_text(json.dumps(report, indent=2) + "\n")
                print("Report: artifacts/reports/openai-compatible-static.json; status=" + report["status"], flush=True)
    assert report["status"] == "PASS"


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Generic upstream check failed; inspect the sanitized report, secrets suppressed") from None
