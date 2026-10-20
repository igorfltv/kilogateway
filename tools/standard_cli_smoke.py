"""Check the exact public login command on installed Kilo/global config, without a launcher.

Requires explicit invocation after make install-kilo. A temporary local test user/credential is
created and cleaned up; existing y.ai auth is never overwritten. Other native credentials
are compared in memory before/after without exposing or copying their secrets.
"""
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

from tools.device_login import approve
from tools.keycloak_test_user import test_user

ROOT = Path(__file__).resolve().parents[1]


async def smoke(client_env, authfile, original, username, password):
    binary = shutil.which("kilo", path=client_env.get("PATH"))
    if not binary:
        raise RuntimeError("Installed kilo was not found")
    if any(client_env.get(k) for k in ("KILO_CONFIG", "KILO_CONFIG_CONTENT", "KILO_CONFIG_DIR", "OPENCODE_CONFIG")):
        raise RuntimeError("Global CLI test requires no explicit config override")
    work = Path(tempfile.mkdtemp(prefix="standard-cli-", dir=ROOT / ".local"))
    key, created_entry = None, None
    log = bytearray()
    process = None
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as client:
        try:
            process = await asyncio.create_subprocess_exec("kilo", "auth", "login", "--provider", "y.ai",
                cwd=ROOT, env=client_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            print("Standard CLI: kilo auth login --provider y.ai (installed CLI; normal profile)", flush=True)
            approved = False
            async with asyncio.timeout(180):
                while chunk := await process.stdout.read(1024):
                    log.extend(chunk)
                    if len(log) > 200000:
                        raise RuntimeError("CLI diagnostic size limit exceeded")
                    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log.decode(errors="replace"))
                    match = re.search(re.escape(os.environ["OIDC_ISSUER"].split("/realms/")[0])
                                      + r"/[^\s]+user_code=[A-Za-z0-9-]+", clean)
                    if match and not approved:
                        await approve(match.group(0), username, password)
                        approved = True
                        print("Standard CLI: Company SSO selected automatically; real Device Flow approved", flush=True)
                status = await process.wait()
            if status or not approved:
                (work / "login-diagnostic.log").write_bytes(log)
                raise RuntimeError("Installed CLI login failed; diagnostic retained only in ignored .local")
            native = json.loads(authfile.read_text())
            created_entry = native.get("y.ai")
            if not created_entry or created_entry.get("type") != "api" or not created_entry.get("key", "").startswith("cg_gc_"):
                raise RuntimeError("Expected native gateway credential was not stored")
            key = created_entry["key"]
            if created_entry.get("metadata") != {"company_gateway":os.environ["GATEWAY_PUBLIC_URL"],
                                                   "company_issuer":os.environ["OIDC_ISSUER"]}:
                raise RuntimeError("Expected public origin binding was not stored")
            if {k:v for k,v in native.items() if k != "y.ai"} != original:
                raise RuntimeError("Other native credentials changed")
            response = await client.get(os.environ["GATEWAY_PUBLIC_URL"] + "/me", headers={"Authorization":"Bearer " + key})
            if response.status_code != 200:
                raise RuntimeError("Stored native credential did not authorize /me")
            print("Standard CLI: global auth store has gateway key and public metadata; /me authorized", flush=True)
        finally:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            if key:
                r = await client.delete(os.environ["GATEWAY_PUBLIC_URL"] + "/auth/cli/credentials/current",
                                        headers={"Authorization":"Bearer " + key})
                if r.status_code != 204:
                    raise RuntimeError("Temporary server credential cleanup failed")
            if created_entry and json.loads(authfile.read_text()).get("y.ai") == created_entry:
                logout = await asyncio.create_subprocess_exec("kilo", "auth", "logout", "y.ai", cwd=ROOT,
                    env=client_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                await asyncio.wait_for(logout.communicate(), timeout=30)
                if logout.returncode:
                    raise RuntimeError("Temporary native credential cleanup failed")
    after = json.loads(authfile.read_text()) if authfile.exists() else {}
    if after != original:
        raise RuntimeError("Native credentials were not restored after temporary login")
    report = {"command":"kilo auth login --provider y.ai", "cli":binary,
              "kilo_version":shutil.which("kilo",path=client_env.get("PATH")),
              "global_profile":True,"requires_method_flag":False,"real_device_flow":"PASS",
              "native_storage_and_me":"PASS","other_credentials_preserved":"PASS","temporary_auth_cleanup":"PASS"}
    version = await asyncio.create_subprocess_exec("kilo", "--version", env=client_env, cwd=ROOT, stdout=asyncio.subprocess.PIPE)
    result, _ = await version.communicate()
    report["kilo_version"] = result.decode().strip()
    (ROOT / "artifacts/reports/standard-cli-smoke.json").write_text(json.dumps(report,indent=2)+"\n")
    print("PASS: exact standard login command, no --method/launcher/XDG overrides; temporary auth cleaned up; other credentials preserved")


async def main():
    # Capture the real client environment before loading server-side .env for the test helper.
    client_env = os.environ.copy()
    load_dotenv(ROOT / ".env")
    if os.environ.get("PROFILE") != "dev":
        raise RuntimeError("Standard CLI smoke is local dev only")
    data = Path(client_env.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    authfile = data / "kilo/auth.json"
    original = json.loads(authfile.read_text()) if authfile.exists() else {}
    if "y.ai" in original:
        print("SKIPPED: existing y.ai auth is preserved; run your normal login directly")
        return
    async with test_user() as (username, password, _, _):
        await smoke(client_env, authfile, original, username, password)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("FAIL: installed CLI smoke; inspect ignored .local diagnostic, secrets suppressed") from None
