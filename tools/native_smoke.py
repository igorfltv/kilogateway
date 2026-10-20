"""Actual pinned Kilo subprocess, isolated XDG roots, actual Keycloak Device Flow and fake inference."""
import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

from tools.device_login import approve
from tools.mcp_login import complete_setup
from tools.keycloak_test_user import test_user

ROOT = Path(__file__).resolve().parents[1]


async def smoke(username, password, binary=None):
    load_dotenv(ROOT / ".env")
    if os.environ.get("PROFILE") != "dev":
        raise RuntimeError("Native smoke is local dev only")
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="native-", dir=ROOT / ".local"))
    env = {k:v for k,v in os.environ.items() if k in {"PATH","HOME","USER","LOGNAME","SHELL","LANG",
        "LC_ALL","TERM","TMPDIR","SSL_CERT_FILE","NODE_EXTRA_CA_CERTS","SYSTEMROOT"}}
    for key in ("CONFIG", "DATA", "STATE", "CACHE"):
        dest = work / key.lower()
        dest.mkdir()
        env[f"XDG_{key}_HOME"] = str(dest)
    env["KILO_DISABLE_DEFAULT_PLUGINS"] = "true"
    env["KILO_DISABLE_MODELS_FETCH"] = "true"
    env["COMPANY_GATEWAY_URL"] = os.environ["GATEWAY_PUBLIC_URL"]
    env["COMPANY_OIDC_ISSUER"] = os.environ["OIDC_ISSUER"]
    env["COMPANY_ALLOW_DEV_HTTP"] = "1"
    conf = json.loads((ROOT / "plugin/examples/kilo.dev.json").read_text())
    conf["plugin"][0][0] = (ROOT / "plugin/dist/index.js").as_uri()
    conf["plugin"][0][1]["openBrowser"] = False  # Headless HTTP form helper owns the test browser flow.
    confpath = work / "config/kilo/kilo.json"
    confpath.parent.mkdir()
    confpath.write_text(json.dumps(conf))
    project = work / "project"
    project.mkdir()
    binary = Path(binary) if binary else ROOT / "node_modules/.bin/kilo"
    log = bytearray()
    p = await asyncio.create_subprocess_exec(str(binary), "auth", "login", "--provider", "y.ai",
        cwd=project, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    approved, mcp_complete = False, False
    print("Native smoke: waiting for actual Kilo Device Flow", flush=True)
    try:
        async with asyncio.timeout(180):
            while True:
                chunk = await p.stdout.read(1024)
                if not chunk:
                    break
                log.extend(chunk)
                if len(log) > 200000:
                    raise RuntimeError("CLI log limit")
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.decode(errors="replace"))
                match = re.search(r"http://localhost:28080/[^\s]+user_code=[A-Za-z0-9-]+", clean)
                if match and not approved:
                    await approve(match.group(0), username, password)
                    approved = True
                    print("Native smoke: real Keycloak device approved", flush=True)
                setup = re.search(r"http://(?:localhost|127\.0\.0\.1):[0-9]+/[^\s]*auth/mcp/setup#ms_[\w-]+\.[\w-]+", clean)
                if setup and not mcp_complete:
                    await complete_setup(setup.group(0))
                    mcp_complete = True
            code = await p.wait()
        if code or not approved:
            # Kilo diagnostic logs may contain sensitive request data; retain locally, never print.
            (work / "login-diagnostic.log").write_bytes(log)
            raise RuntimeError("Native login failed; isolated diagnostic location: " + str(work))
    finally:
        if p.returncode is None:
            p.terminate()
            await p.wait()
    authfile = work / "data/kilo/auth.json"
    native = json.loads(authfile.read_text())["y.ai"]
    assert native["type"] == "api" and native["key"].startswith("cg_gc_")
    assert set(native) == {"type", "key", "metadata"}
    assert native["metadata"] == {"company_gateway": os.environ["GATEWAY_PUBLIC_URL"], "company_issuer": os.environ["OIDC_ISSUER"]}
    key = native["key"]
    print("Native smoke: login stored only gateway API credential", flush=True)
    expected_models = ["y.ai/" + m for m in conf["provider"]["y.ai"]["models"]]
    proc = await asyncio.create_subprocess_exec(str(binary), "models", "y.ai", cwd=project, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
    assert proc.returncode == 0 and key.encode() not in output
    listed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output.decode()).splitlines()
    assert set(expected_models) == {line.strip() for line in listed if line.startswith("y.ai/")}
    print("Native smoke: actual Kilo model list matches configured catalog", flush=True)
    for index, model in enumerate(expected_models):
        await run_model(binary, model, project, env, work / f"run-{index}-diagnostic.log", key)
    print("Native smoke: kilo run received fake streaming responses for all models", flush=True)
    # Native logout is a separate local action. Check it leaves server credential valid.
    proc = await asyncio.create_subprocess_exec(str(binary), "auth", "logout", "y.ai", cwd=project, env=env,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 0 and "y.ai" not in json.loads(authfile.read_text())
    async with httpx.AsyncClient(trust_env=False) as c:
        h = {"Authorization": "Bearer " + key}
        assert (await c.get(env["COMPANY_GATEWAY_URL"] + "/me", headers=h)).status_code == 200
        assert (await c.delete(env["COMPANY_GATEWAY_URL"] + "/auth/cli/credentials/current", headers=h)).status_code == 204
        assert (await c.get(env["COMPANY_GATEWAY_URL"] + "/me", headers=h)).status_code == 401
    print("PASS: native Kilo login/models/run/storage/logout and separate server revoke; all inference was FAKE")
    version = await asyncio.create_subprocess_exec(str(binary), "--version", cwd=project, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    version_output, _ = await asyncio.wait_for(version.communicate(), timeout=30)
    assert version.returncode == 0
    report = {"kilo": version_output.decode().strip(), "plugin": "0.1.0", "provider": "fake", "native_login": "PASS",
              "command": "kilo auth login --provider y.ai", "isolated_client_state": True,
              "native_models": "PASS", "models": expected_models,
              "native_run": "PASS", "gateway_only_storage": "PASS", "local_logout": "PASS", "server_revoke": "PASS"}
    (ROOT / "artifacts/reports/native-smoke.json").write_text(json.dumps(report, indent=2))


async def run_model(binary, model, project, env, log_path, key):
    proc = await asyncio.create_subprocess_exec(str(binary), "run", "--format", "json", "--model", model,
        "Reply OK without tools", cwd=project, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        async with asyncio.timeout(180):
            output, _ = await proc.communicate()
        log_path.write_bytes(output)
        assert proc.returncode == 0 and b"FAKE:" in output
        assert key.encode() not in output
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


async def main():
    load_dotenv(ROOT / ".env")
    async with test_user() as (username, password, _, _):
        await smoke(username, password)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        raise SystemExit("FAIL: native smoke: " + (str(e) if isinstance(e, (RuntimeError, AssertionError)) else type(e).__name__)) from None
