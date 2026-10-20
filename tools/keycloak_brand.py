"""Apply the local Y.AI login theme without changing issuer, clients, users or credentials."""
import asyncio
import os
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


async def main():
    load_dotenv()
    issuer = os.environ["OIDC_ISSUER"]
    if os.environ.get("PROFILE") != "dev" or urlparse(issuer).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Branding helper is restricted to the local development realm")
    base, realm = issuer.rsplit("/realms/", 1)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as c:
        for _ in range(60):
            try:
                r = await c.get(issuer + "/.well-known/openid-configuration")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Keycloak readiness timeout")
        r = await c.post(base + "/realms/master/protocol/openid-connect/token", data={
            "client_id":"admin-cli", "grant_type":"password", "username":os.environ["KC_ADMIN_USER"],
            "password":os.environ["KC_ADMIN_PASSWORD"]})
        r.raise_for_status()
        headers = {"Authorization":"Bearer " + r.json()["access_token"]}
        r = await c.put(base + "/admin/realms/" + realm, headers=headers,
                        json={"displayName":"Y.AI", "displayNameHtml":"Y.AI", "loginTheme":"y-ai"})
        r.raise_for_status()
        r = await c.get(base + "/admin/realms/" + realm, headers=headers)
        r.raise_for_status()
        data = r.json()
        if data.get("displayName") != "Y.AI" or data.get("loginTheme") != "y-ai":
            raise RuntimeError("Branding was not applied")
    print("PASS: local realm display name Y.AI and login theme y-ai applied; identity/configuration unchanged")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Y.AI branding failed; check local Keycloak readiness (secrets suppressed)") from None
