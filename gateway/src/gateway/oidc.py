import asyncio
import time
from urllib.parse import urlparse

import httpx
import jwt

from gateway.errors import GatewayError


class Identity:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.s, self.client = settings, client
        self.discovery = None
        self.keys, self.keys_at, self.last_unknown = {}, 0.0, 0.0
        self.lock = asyncio.Lock()
        self.service_token, self.service_until = "", 0.0
        self.service_lock = asyncio.Lock()

    def route(self, url: str):
        issuer = urlparse(self.s.oidc_issuer)
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in {f"{issuer.scheme}://{issuer.netloc}", *self.s.oidc_allowed_origins} or u.username or u.password:
            raise GatewayError("oidc_untrusted_endpoint")
        if self.s.oidc_internal_origin and (u.scheme, u.netloc) == (issuer.scheme, issuer.netloc):
            return self.s.oidc_internal_origin.rstrip("/") + u.path + ("?" + u.query if u.query else "")
        return url

    async def discover(self):
        if self.discovery:
            return self.discovery
        r = await self.client.get(self.route(self.s.oidc_discovery_url or self.s.oidc_issuer + "/.well-known/openid-configuration"))
        r.raise_for_status()
        d = r.json()
        if d.get("issuer") != self.s.oidc_issuer:
            raise GatewayError("oidc_issuer_mismatch")
        d.update({key: value for key, value in {"token_endpoint": self.s.oidc_token_url,
                  "jwks_uri": self.s.oidc_jwks_url}.items() if value})
        for field in ("jwks_uri", "token_endpoint"):
            self.route(d[field])
        self.discovery = d
        return d

    async def verify(self, token: str):
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError()
            async with self.lock:
                now = time.monotonic()
                unknown = header["kid"] not in self.keys
                if now - self.keys_at > 300 or (unknown and now - self.last_unknown > 2):
                    d = await self.discover()
                    r = await self.client.get(self.route(d["jwks_uri"]))
                    r.raise_for_status()
                    self.keys = {k["kid"]: jwt.PyJWK(k, algorithm="RS256").key for k in r.json()["keys"]
                                 if k.get("kty") == "RSA" and k.get("use", "sig") == "sig"}
                    self.keys_at, self.last_unknown = now, now
            key = self.keys.get(header["kid"])
            if not key:
                raise ValueError()
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.s.oidc_issuer,
                                audience=self.s.oidc_audience,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            # Keycloak ID tokens have typ=ID and must never be accepted as access tokens.
            if claims.get("typ") != "Bearer" or claims.get("azp") != self.s.oidc_client_id:
                raise ValueError()
            roles = set(claims.get("realm_access", {}).get("roles", []))
            if not {"invoke", "issue-credential"} <= roles:
                raise GatewayError("role_required", 403)
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError()
            return claims
        except GatewayError:
            raise
        except (jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise GatewayError("invalid_access_token", 401) from None
        except httpx.HTTPError:
            raise GatewayError("identity_unavailable") from None

    async def status(self, subject: str) -> bool:
        """Admin API is the concrete synchronization mechanism; no stale-on-error fallback."""
        try:
            d = await self.discover()
            async with self.service_lock:
                if time.monotonic() >= self.service_until:
                    r = await self.client.post(self.route(d["token_endpoint"]), data={
                        "grant_type": "client_credentials", "client_id": self.s.keycloak_status_client_id,
                        "client_secret": self.s.keycloak_status_client_secret.get_secret_value()})
                    r.raise_for_status()
                    t = r.json()
                    self.service_token = t["access_token"]
                    self.service_until = time.monotonic() + max(1, int(t["expires_in"]) - 10)
            realm_path = self.s.oidc_issuer.rsplit("/realms/", 1)
            if len(realm_path) != 2:
                raise GatewayError("keycloak_realm_required")
            from urllib.parse import quote
            admin_base = self.s.keycloak_admin_base_url or realm_path[0] + "/admin/realms/" + quote(realm_path[1], safe="")
            base = self.route(admin_base + "/users/" + quote(subject, safe=""))
            h = {"Authorization": "Bearer " + self.service_token}
            r = await self.client.get(base, headers=h)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            if not r.json().get("enabled", False):
                return False
            r = await self.client.get(base + "/role-mappings/realm/composite", headers=h)
            r.raise_for_status()
            return {"invoke", "issue-credential"} <= {x["name"] for x in r.json()}
        except GatewayError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise GatewayError("identity_status_unavailable") from None
