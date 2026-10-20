import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()


async def bootstrap():
    if os.environ.get("PROFILE") != "dev":
        raise RuntimeError("Development bootstrap requires PROFILE=dev")
    issuer = os.environ["OIDC_ISSUER"]
    base, realm = issuer.rsplit("/realms/", 1)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=15) as client:
        for i in range(60):
            try:
                r = await client.get(issuer + "/.well-known/openid-configuration")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Keycloak readiness timeout")
        r = await client.post(base + "/realms/master/protocol/openid-connect/token", data={
            "client_id": "admin-cli", "grant_type": "password", "username": os.environ["KC_ADMIN_USER"],
            "password": os.environ["KC_ADMIN_PASSWORD"]})
        r.raise_for_status()
        token = r.json()["access_token"]

        async def api(method, path, data=None):
            r = await client.request(method, base + f"/admin/realms/{realm}" + path,
                    json=data, headers={"Authorization": "Bearer " + token})
            r.raise_for_status()
            return r.json() if r.content else None

        await api("PUT", "", {"displayName":"Y.AI", "displayNameHtml":"Y.AI", "loginTheme":"y-ai"})

        users = {}
        for name, password_env, allowed in (("developer", "DEV_USER_PASSWORD", True),
                                           ("denied", "DEV_DENIED_PASSWORD", False)):
            found = await api("GET", f"/users?username={name}&exact=true")
            if not found:
                await api("POST", "/users", {"username": name, "enabled": True, "emailVerified": True,
                          "firstName": name, "lastName": "Local", "email": name + "@example.invalid"})
                found = await api("GET", f"/users?username={name}&exact=true")
            ident = found[0]["id"]
            users[name] = ident
            await api("PUT", f"/users/{ident}/reset-password", {"type": "password", "temporary": False,
                                                               "value": os.environ[password_env]})
            roles = [await api("GET", f"/roles/{name}") for name in ("invoke", "issue-credential")]
            await api("POST" if allowed else "DELETE", f"/users/{ident}/role-mappings/realm", roles)
        clients = await api("GET", "/clients?clientId=gateway-status")
        service_id = clients[0]["id"]
        await api("PUT", f"/clients/{service_id}", {**clients[0], "secret": os.environ["KEYCLOAK_STATUS_CLIENT_SECRET"]})
        service = await api("GET", f"/clients/{service_id}/service-account-user")
        management = (await api("GET", "/clients?clientId=realm-management"))[0]["id"]
        old_roles = await api("GET", f"/users/{service['id']}/role-mappings/clients/{management}")
        extra = [role for role in old_roles if role["name"] in {"query-users", "view-realm"}]
        if extra:
            await api("DELETE", f"/users/{service['id']}/role-mappings/clients/{management}", extra)
        roles = [await api("GET", f"/clients/{management}/roles/view-users")]
        await api("POST", f"/users/{service['id']}/role-mappings/clients/{management}", roles)
    print("Keycloak ready: developer (allowed), denied (no roles), gateway-status (read-only status). No secrets printed.")
    return users


if __name__ == "__main__":
    try:
        asyncio.run(bootstrap())
    except Exception:
        raise SystemExit("Keycloak bootstrap failed; check service readiness and .env (secrets suppressed)") from None
