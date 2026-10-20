"""Real OpenCode v2 Device Flow and inference, with isolated client state and owned-key cleanup."""

import asyncio
import json
import os
import re
import shlex
import socket
import shutil
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

from gateway.config import Settings
from gateway.db import DB, rows, run
from providers.openrouter import OpenRouterAdapter
from tools.keycloak_test_user import test_user
from tools.live_validate import native_login, regular_state
from tools.opencode_config import build_config

ROOT = Path(__file__).resolve().parents[1]


async def main():
    if os.environ.get("ENABLE_REAL_INFERENCE_CHECKS") != "1":
        print("SKIPPED: set ENABLE_REAL_INFERENCE_CHECKS=1 for OpenCode native checks")
        return
    native_path = os.environ.get("YAI_OPENCODE_BINARY")
    if not native_path:
        runtime = Path.home() / ".local/share/y-ai/opencode/runtime.json"
        native_path = json.loads(runtime.read_text())["binary"]
    native = Path(native_path).resolve()
    baseline = regular_state()
    s = Settings(oidc_internal_origin="")
    assert s.profile == "dev" and s.credential_provider == "openrouter-managed"
    work = Path(tempfile.mkdtemp(prefix="opencode-native-", dir=ROOT / ".local"))
    work.chmod(0o700)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "NODE_EXTRA_CA_CERTS",
            "SSL_CERT_FILE",
        }
    }
    for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
        dest = work / kind.lower()
        dest.mkdir()
        env["XDG_" + kind + "_HOME"] = str(dest)
    env["YAI_OPENCODE_BINARY"] = str(native)
    config = build_config(s)
    config["plugins"][0]["options"]["openBrowser"] = False
    config_path = work / "config/opencode/opencode.jsonc"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(config))
    project = work / "project"
    project.mkdir()
    bin_dir = work / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "opencode"
    binary.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(str(ROOT / "node_modules/.bin/node"))
        + " "
        + shlex.quote(str(ROOT / "plugin/dist/opencode-cli.js"))
        + ' "$@"\n'
    )
    binary.chmod(0o700)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    report = {
        "status": "RUNNING",
        "command": "opencode auth login --provider y.ai",
        "checks": {},
        "models": {},
        "isolated_client_state": True,
        "synthetic_prompts_only": True,
    }
    subject = None
    key = None

    def passed(name):
        report["checks"][name] = "PASS"
        print("PASS: " + name, flush=True)

    async def command(args, secret_output=False, timeout=120):
        process = await asyncio.create_subprocess_exec(
            str(binary),
            *args,
            cwd=project,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                if not secret_output:
                    diagnostic = work / "command-private.log"
                    diagnostic.write_bytes(out + b"\n" + err)
                    diagnostic.chmod(0o600)
                raise RuntimeError("opencode_command_failed")
            return out
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()

    async def registry_check():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = work / "registry-server-private.log"
        log.touch(mode=0o600)
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                str(binary),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
                cwd=project,
                env=env,
                stdout=output,
                stderr=output,
            )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as api:
                for _ in range(100):
                    password = re.search(
                        r"^server password (\S+)$", log.read_text(), re.M
                    )
                    if password:
                        try:
                            options = {
                                "params": {"location[directory]": str(project)},
                                "auth": ("opencode", password.group(1)),
                            }
                            plugins = (
                                (
                                    await api.get(
                                        f"http://127.0.0.1:{port}/api/plugin", **options
                                    )
                                )
                                .json()
                                .get("data", [])
                            )
                            active = any(
                                p.get("id") == "company.y-ai.auth"
                                and p["state"]["status"] == "active"
                                for p in plugins
                            )
                            models = (
                                (
                                    await api.get(
                                        f"http://127.0.0.1:{port}/api/model", **options
                                    )
                                )
                                .json()
                                .get("data", [])
                            )
                            ids = {
                                m["id"] for m in models if m.get("providerID") == "y.ai"
                            }
                            if active and ids == {m.id for m in s.models}:
                                passed("native_v2_plugin_registry_active")
                                passed("native_v2_model_catalog")
                                return
                        except (httpx.HTTPError, ValueError, KeyError):
                            pass
                    await asyncio.sleep(0.1)
            raise RuntimeError("native_registry_not_settled")
        finally:
            process.terminate()
            await asyncio.wait_for(process.wait(), 15)

    db = DB(s.database_url.get_secret_value(), s.database_schema)
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=30
    ) as client:
        provider = OpenRouterAdapter(
            client,
            s.openrouter_base_url,
            s.openrouter_management_key.get_secret_value(),
            **s.provider_options,
        )
        load_dotenv(ROOT / ".env")
        try:
            version = (await command(["--version"])).decode().strip()
            assert version == "opencode v2.0.26"
            report["opencode_version"] = version
            async with test_user() as (username, password, subject, _):
                print("OpenCode native: waiting for actual Device Flow", flush=True)
                await native_login(
                    str(binary), project, env, s.oidc_issuer, username, password
                )
                passed("exact_login_command_and_real_device_flow")
                data = json.loads(
                    await command(["api", "GET", "/api/credential"], secret_output=True)
                )["data"]
                accounts = [
                    record for record in data if record["integrationID"] == "y.ai"
                ]
                assert len(accounts) == 1
                account = accounts[0]
                value = account["value"]
                key = value["access"]
                assert (
                    value["type"] == "oauth"
                    and key.startswith("cg_gc_")
                    and value["refresh"] == ""
                )
                assert value["metadata"] == {
                    "company_gateway": s.gateway_public_url,
                    "company_issuer": s.oidc_issuer,
                }
                assert s.openrouter_management_key.get_secret_value() not in json.dumps(
                    data
                )
                passed("native_v2_credential_contains_gateway_key_only")
                response = await client.get(
                    s.gateway_public_url + "/me",
                    headers={"Authorization": "Bearer " + key},
                )
                assert response.status_code == 200
                passed("gateway_authorization")
                for model in s.models:
                    output = await command(
                        [
                            "run",
                            "--format",
                            "json",
                            "--model",
                            "y.ai/" + model.id,
                            "Reply with exactly OK. Do not call tools.",
                        ],
                        timeout=180,
                    )
                    events = []
                    for line in output.decode().splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            pass
                    if not any(event.get("type") == "text" for event in events):
                        diagnostic = work / "run-private.log"
                        diagnostic.write_bytes(output)
                        diagnostic.chmod(0o600)
                        report["event_types"] = sorted(
                            {str(event.get("type")) for event in events}
                        )
                        raise RuntimeError("native_text_event_missing")
                    assert (
                        b'"type":"error"' not in output
                        and b"FAKE:" not in output
                        and key.encode() not in output
                    )
                    report["models"][model.id] = "PASS"
                    passed("real_native_run:" + model.id)
                await registry_check()
                await command(["auth", "logout", "y.ai", account["id"]])
                remaining = json.loads(
                    await command(["api", "GET", "/api/credential"], secret_output=True)
                )["data"]
                assert not any(x["integrationID"] == "y.ai" for x in remaining)
                assert (
                    await client.get(
                        s.gateway_public_url + "/me",
                        headers={"Authorization": "Bearer " + key},
                    )
                ).status_code == 200
                assert (
                    await client.delete(
                        s.gateway_public_url + "/auth/cli/credentials/current",
                        headers={"Authorization": "Bearer " + key},
                    )
                ).status_code == 204
                assert (
                    await client.get(
                        s.gateway_public_url + "/me",
                        headers={"Authorization": "Bearer " + key},
                    )
                ).status_code == 401
                passed("native_logout_and_separate_gateway_revoke")
            report["status"] = "PASS"
        except Exception as error:
            report["status"] = "FAIL"
            report["error"] = (
                str(error) if isinstance(error, RuntimeError) else type(error).__name__
            )
            raise
        finally:
            # Scope cleanup by the newly created Keycloak subject, never by labels belonging to another user.
            if subject:
                async with db.tx() as conn:
                    credentials = await rows(
                        conn,
                        """SELECT pc.id,pc.provider_reference FROM provider_credentials pc
                        JOIN principals p ON p.id=pc.principal_id WHERE p.subject=:s AND p.issuer=:i
                        AND pc.provider='openrouter-managed'""",
                        s=subject,
                        i=s.oidc_issuer,
                    )
                removed = True
                for record in credentials:
                    await provider.revoke(record["provider_reference"])
                    response = await client.get(
                        provider.management_base
                        + provider.keys_path
                        + "/"
                        + record["provider_reference"],
                        headers={
                            "Authorization": "Bearer "
                            + s.openrouter_management_key.get_secret_value()
                        },
                    )
                    removed = removed and response.status_code == 404
                    if response.status_code == 404:
                        async with db.tx() as conn:
                            await run(
                                conn,
                                "UPDATE provider_credentials SET state='revoked',ciphertext=NULL WHERE id=:id",
                                id=record["id"],
                            )
                async with db.tx() as conn:
                    await run(
                        conn,
                        """UPDATE gateway_credentials SET revoked_at=COALESCE(revoked_at,now()) WHERE principal_id IN
                        (SELECT id FROM principals WHERE subject=:s AND issuer=:i)""",
                        s=subject,
                        i=s.oidc_issuer,
                    )
                report["owned_upstream_keys_deleted"] = removed
                if not removed:
                    report["status"] = "FAIL"
            await command(["service", "stop"], timeout=30)
            report["regular_kilo_state_preserved"] = regular_state() == baseline
            if not report["regular_kilo_state_preserved"]:
                report["status"] = "FAIL"
            await db.engine.dispose()
            Path(ROOT / "artifacts/reports/opencode-native.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(
                "Report: artifacts/reports/opencode-native.json; status="
                + report["status"],
                flush=True,
            )
            if report["status"] == "PASS":
                shutil.rmtree(work)
    assert report["status"] == "PASS"


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit(
            "OpenCode validation failed; inspect the sanitized report and private diagnostic, secrets suppressed"
        ) from None
