"""Real Keycloak + both native CLIs + fake Data Center MCPs, isolated state, no inference."""
import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlencode

import httpx
import uvicorn
from dotenv import dotenv_values

from gateway.app import create_app
from gateway.config import Settings
from gateway.db import DB, rows, run
from gateway.schema import initialize_test_schema
from tools.device_login import approve
from tools.keycloak_test_user import test_user
from tools.kilo_config import build_config as kilo_config
from tools.live_validate import client_environment
from tools.mcp_login import complete_setup
from tools.opencode_config import build_config as opencode_config

ROOT = Path(__file__).resolve().parents[1]


async def login(command, project, env, issuer, username, password):
    process = await asyncio.create_subprocess_exec(*command, "auth", "login", "--provider", "y.ai",
        cwd=project, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    log, approved, submitted = bytearray(), False, False
    try:
        async with asyncio.timeout(150):
            while chunk := await process.stdout.read(2048):
                log.extend(chunk)
                if len(log) > 200000:
                    raise RuntimeError("Native login output exceeded limit")
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.decode(errors="replace"))
                sso = re.search(re.escape(issuer.split("/realms/")[0]) + r"/[^\s]+user_code=[A-Za-z0-9-]+", clean)
                if sso and not approved:
                    await approve(sso.group(0), username, password)
                    approved = True
                    print("PASS: native SSO approval", flush=True)
                setup = re.search(r"http://127\.0\.0\.1:[0-9]+/auth/mcp/setup#ms_[\w-]+\.[\w-]+", clean)
                if setup and not submitted:
                    await complete_setup(setup.group(0), fake_pats=True)
                    submitted = True
                    print("PASS: native MCP PAT form submitted", flush=True)
            if await process.wait() or not approved or not submitted:
                raise RuntimeError("Native login or PAT setup failed")
    except BaseException:
        safe = re.sub(r"(?:cg_gc_|ms_)[A-Za-z0-9._-]+", "[REDACTED]", log.decode(errors="replace"))
        (project.parent / "native-login-private.log").write_text(safe)
        raise
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def command(args, project, env):
    process = await asyncio.create_subprocess_exec(*args, cwd=project, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        if process.returncode:
            (project.parent / "native-command-private.log").write_bytes(output)
            raise RuntimeError("Native MCP command failed")
        return output.decode(errors="replace")
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def main(browser_check=False):
    work = Path(tempfile.mkdtemp(prefix="mcp-smoke-", dir=ROOT / ".local"))
    work.chmod(0o700)
    schema = "mcp_smoke_" + uuid4().hex
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    s = Settings(profile="dev", gateway_public_url=base, database_schema=schema, oidc_internal_origin="",
        provider_mode="openai-compatible", provider_credential_mode="static", provider_api_key="SYNTHETIC_NOT_USED",
        provider_inference_base_url="http://localhost:18090/v1", jira_base_url="http://localhost:18091/jira",
        confluence_base_url="http://localhost:18091/confluence")
    local = dotenv_values(ROOT / ".env")
    for key in ("KC_ADMIN_USER", "KC_ADMIN_PASSWORD"):
        os.environ[key] = local[key]
    os.environ.update(PROFILE="dev", OIDC_ISSUER=s.oidc_issuer)
    setup = DB(s.database_url.get_secret_value())
    db = DB(s.database_url.get_secret_value(), schema)
    server, task = None, None
    managed_clients = []
    report = {"status": "RUNNING", "real_keycloak": True, "fake_atlassian": True, "inference_calls": 0,
              "provider_keys_created": 0, "checks": {}}
    try:
        async with setup.tx() as c:
            await run(c, f'CREATE SCHEMA "{schema}"')
        async with db.tx() as c:
            await initialize_test_schema(c, ROOT / "gateway/migrations")
        app = create_app(s, worker=False)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.1)
        assert server.started
        last_key = None
        async with test_user() as (username, password, _, _):
            for kind in ("kilo", "opencode"):
                client_work = work / kind
                client_work.mkdir()
                project = client_work / "project"
                project.mkdir()
                env = client_environment(client_work)
                conf = kilo_config(s) if kind == "kilo" else opencode_config(s)
                (conf["plugin"][0][1] if kind == "kilo" else conf["plugins"][0]["options"])["openBrowser"] = False
                configfile = client_work / f"config/{kind}/{kind}.jsonc"
                configfile.parent.mkdir()
                configfile.write_text(json.dumps(conf))
                if kind == "kilo":
                    binary = [shutil.which("kilo")]
                else:
                    runtime = json.loads((Path.home() / ".local/share/y-ai/opencode/runtime.json").read_text())
                    env["YAI_OPENCODE_BINARY"] = runtime["binary"]
                    binary = [str(ROOT / "node_modules/.bin/node"), str(ROOT / "plugin/dist/opencode-cli.js")]
                    with socket.socket() as service_sock:
                        service_sock.bind(("127.0.0.1", 0))
                        service_port = service_sock.getsockname()[1]
                    await command([*binary, "service", "set", "port", str(service_port)], project, env)
                    managed_clients.append((binary, project, env))
                await login(binary, project, env, s.oidc_issuer, username, password)
                report["checks"][kind + "_sso_pat_login"] = "PASS"
                if kind == "kilo":
                    auth = json.loads((client_work / "data/kilo/auth.json").read_text())["y.ai"]
                    last_key = auth["key"]
                    output = await command([*binary, "mcp", "list"], project, env)
                    if not ("y-ai-jira" in output and "y-ai-confluence" in output and "connected" in output.lower()):
                        (client_work / "mcp-list-private.log").write_text(output)
                        raise RuntimeError("Kilo native MCP registry did not connect")
                else:
                    path = "/api/mcp?" + urlencode({"location[directory]": str(project)})
                    for _ in range(20):
                        output = await command([*binary, "api", "GET", path], project, env)
                        if "y-ai-jira" in output and "y-ai-confluence" in output and "connected" in output:
                            break
                        await asyncio.sleep(.25)
                    if not ("y-ai-jira" in output and "y-ai-confluence" in output and "connected" in output):
                        (client_work / "mcp-list-private.log").write_text(output)
                        raise RuntimeError("OpenCode native MCP registry did not connect")
                    await command([*binary, "service", "stop"], project, env)
                    managed_clients.pop()
                report["checks"][kind + "_native_mcp_registry"] = "PASS"
                for p in client_work.rglob("*"):
                    if p.is_file() and p.stat().st_size < 30000000:
                        content = p.read_bytes()
                        if b"fake-jira-pat" in content or b"fake-confluence-pat" in content:
                            raise RuntimeError("Synthetic PAT persisted in client state")
                report["checks"][kind + "_no_pat_in_client_state"] = "PASS"
                print(f"PASS: {kind}: real SSO + PAT setup + native MCP connected + no PAT on disk", flush=True)
            async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
                headers = {"Authorization": "Bearer " + last_key}
                if browser_check:
                    form = (await c.post(base + "/auth/mcp/setup", headers=headers)).json()
                    (work / "browser.json").write_text(json.dumps({"url": form["url"], "done": str(work / "browser.done")}))
                    print("BROWSER_CHECK_READY " + str(work / "browser.json"), flush=True)
                    for _ in range(1200):
                        if (work / "browser.done").exists():
                            break
                        await asyncio.sleep(.25)
                    status = await c.get(base + "/auth/mcp/setup/status/" + form["id"], headers=headers)
                    assert status.json()["complete"] is True
                    report["checks"]["rendered_pat_form"] = "PASS"
                for system, name, args in [("jira", "get_issue", {"key": "DEMO-1"}), ("confluence", "get_page", {"id": "1001"})]:
                    result = (await c.post(base + "/mcp/" + system, headers=headers,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})).json()
                    assert result["result"]["isError"] is False and "FAKE" in result["result"]["content"][0]["text"]
                report["checks"]["http_mcp_tools_executed"] = "PASS"
        async with db.tx() as c:
            credentials = await rows(c, "SELECT ciphertext FROM mcp_credentials")
            assert len(credentials) == 2 and all("fake-" not in row["ciphertext"] for row in credentials)
        report["checks"]["server_ciphertext_only"] = "PASS"
        report["status"] = "PASS"
    finally:
        for binary, project, env in managed_clients:
            await command([*binary, "service", "stop"], project, env)
        if server:
            server.should_exit = True
        if task:
            await asyncio.wait_for(task, 15)
        sock.close()
        await db.engine.dispose()
        async with setup.tx() as c:
            await run(c, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.engine.dispose()
        if report["status"] == "PASS":
            shutil.rmtree(work)
        else:
            report["status"] = "FAIL"
            print("Private diagnostics retained in " + str(work), flush=True)
        (ROOT / "artifacts/reports/mcp-onboarding.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: isolated MCP end-to-end validation; no inference or paid keys", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-check", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.browser_check))
