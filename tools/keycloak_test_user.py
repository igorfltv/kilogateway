"""Create and delete a distinct local-realm test user; never touch an existing employee."""
import os
import secrets
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx


@asynccontextmanager
async def test_user(allowed=True):
    if os.environ.get("PROFILE") != "dev":
        raise RuntimeError("Test user creation is restricted to dev")
    base, realm = os.environ["OIDC_ISSUER"].rsplit("/realms/", 1)
    if not base.startswith("http://localhost:"):
        raise RuntimeError("Test user creation is restricted to loopback")
    async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
        async def admin_token():
            r = await c.post(base + "/realms/master/protocol/openid-connect/token", data={
                "client_id": "admin-cli", "grant_type": "password", "username": os.environ["KC_ADMIN_USER"],
                "password": os.environ["KC_ADMIN_PASSWORD"]})
            r.raise_for_status()
            return r.json()["access_token"]
        token = await admin_token()

        async def admin(method, path, body=None):
            nonlocal token
            r = await c.request(method, base + f"/admin/realms/{realm}" + path, json=body,
                                headers={"Authorization": "Bearer " + token})
            if r.status_code == 401:
                token = await admin_token()
                r = await c.request(method, base + f"/admin/realms/{realm}" + path, json=body,
                                    headers={"Authorization": "Bearer " + token})
            r.raise_for_status()
            return r.json() if r.content else None

        name, password = "smoke-" + uuid4().hex, secrets.token_urlsafe(24)
        await admin("POST", "/users", {"username": name, "enabled": True, "emailVerified": True,
            "firstName": "Smoke", "lastName": "Local", "email": name + "@example.invalid",
            "credentials": [{"type": "password", "temporary": False, "value": password}]})
        ident = (await admin("GET", f"/users?username={name}&exact=true"))[0]["id"]
        try:
            if allowed:
                roles = [await admin("GET", "/roles/" + role) for role in ("invoke", "issue-credential")]
                await admin("POST", f"/users/{ident}/role-mappings/realm", roles)
            yield name, password, ident, admin
        finally:
            await admin("DELETE", "/users/" + ident)
