import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from gateway.broker import uid, utcnow
from gateway.db import audit, one, rows, run
from gateway.errors import GatewayError
from gateway.secrets import digest

AUTH_FIELDS = {
    "g": ("id", "principal_id", "secret_hash", "device_name", "expires_at", "revoked_at", "created_at"),
    "p": ("id", "tenant", "issuer", "subject", "status", "policy_id", "authz_checked_at", "authz_allowed",
          "provider_allocated", "usage_charged_units", "usage_requests"),
    "q": ("id", "aliases", "providers", "rate_limit", "concurrency", "daily_units", "total_units", "provider_budget"),
}
AUTH_QUERY = "SELECT " + ",".join(f"{table}.{name} AS {table}_{name}" for table, fields in AUTH_FIELDS.items()
                                   for name in fields) + """ FROM gateway_credentials g
    JOIN principals p ON p.id=g.principal_id JOIN policies q ON q.id=p.policy_id WHERE g.id=:id"""


class Auth:
    def __init__(self, settings, db, identity, broker, store):
        self.s, self.db, self.identity, self.broker, self.store = settings, db, identity, broker, store
        self.status_checks = {}

    async def close(self):
        tasks = list(self.status_checks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def initialize(self):
        async with self.db.tx() as c:
            await run(c, """INSERT INTO policies(id,aliases,providers,rate_limit,concurrency,daily_units,total_units,provider_budget)
                VALUES ('default',:a,:p,:r,:c,:d,:t,:b) ON CONFLICT DO NOTHING""",
                a=[m.id for m in self.s.models], p=[self.s.provider_mode], r=self.s.request_rate, c=self.s.request_concurrency,
                d=self.s.daily_token_units, t=self.s.total_token_units, b=self.s.principal_provider_budget_usd)

    async def principal(self, claims):
        async with self.db.tx() as c:
            await run(c, """INSERT INTO principals(id,tenant,issuer,subject,policy_id)
                VALUES (:id,:t,:i,:s,'default') ON CONFLICT(tenant,issuer,subject) DO NOTHING""",
                id=uid("user_"), t=self.s.tenant, i=self.s.oidc_issuer, s=claims["sub"])
            p = await one(c, "SELECT * FROM principals WHERE tenant=:t AND issuer=:i AND subject=:s",
                          t=self.s.tenant, i=self.s.oidc_issuer, s=claims["sub"])
        await self.check_status(p)
        return p

    async def check_status(self, p):
        if p["status"] != "active":
            raise GatewayError("principal_suspended", 403)
        fresh = p["authz_checked_at"] and (utcnow() - p["authz_checked_at"]).total_seconds() < self.s.authz_max_staleness_seconds
        if fresh:
            if not p["authz_allowed"]:
                raise GatewayError("corporate_access_revoked", 403)
            return
        # One refresh per principal per process; the database timestamp is shared
        # by all replicas. No DB connection is held while calling the IdP.
        task = self.status_checks.get(p["id"])
        if task is None:
            task = asyncio.create_task(self.refresh_status(p["id"]))
            self.status_checks[p["id"]] = task
            def completed(finished):
                self.status_checks.pop(p["id"], None)
                if not finished.cancelled():
                    finished.exception()  # Retrieve exceptions even if all waiting clients disconnect.
            task.add_done_callback(completed)
        await asyncio.shield(task)

    async def refresh_status(self, owner):
        async with self.db.tx() as c:
            p = await one(c, "SELECT * FROM principals WHERE id=:id", id=owner)
        if not p or p["status"] != "active":
            raise GatewayError("principal_suspended", 403)
        if p["authz_checked_at"] and (utcnow() - p["authz_checked_at"]).total_seconds() < self.s.authz_max_staleness_seconds:
            if not p["authz_allowed"]:
                raise GatewayError("corporate_access_revoked", 403)
            return
        # Check timestamp is measured BEFORE network I/O: latency never extends stale authorization.
        started = utcnow()
        allowed = await self.identity.status(p["subject"])
        async with self.db.tx() as c:
            await run(c, """UPDATE principals SET authz_checked_at=:at,authz_allowed=:ok
                WHERE id=:id AND (authz_checked_at IS NULL OR authz_checked_at<:at)""",
                at=started, ok=allowed, id=p["id"])
            if not allowed:
                await run(c, "UPDATE gateway_credentials SET revoked_at=COALESCE(revoked_at,now()) WHERE principal_id=:p", p=p["id"])
                pcs = await rows(c, "SELECT id FROM provider_credentials WHERE principal_id=:p AND state='active'", p=p["id"])
                for pc in pcs:
                    await self.broker.enqueue_revoke(c, pc["id"])
                await audit(c, p["id"], "identity.status", p["id"], "denied", uid("authz_"))
        if not allowed:
            raise GatewayError("corporate_access_revoked", 403)
        if (utcnow() - started).total_seconds() >= self.s.authz_max_staleness_seconds:
            raise GatewayError("identity_status_too_old")
        async with self.db.tx() as c:
            current = await one(c, "SELECT status,authz_allowed FROM principals WHERE id=:id", id=p["id"])
        if current["status"] != "active" or not current["authz_allowed"]:
            raise GatewayError("corporate_access_revoked", 403)

    async def exchange(self, token, idem, device, client):
        claims = await self.identity.verify(token)
        p = await self.principal(claims)
        owner = p["id"]
        request_hash = hashlib.sha256(json.dumps([device, client]).encode()).hexdigest()
        async with self.broker.lock(owner + ":exchange"):
            async with self.db.tx() as c:
                prev = await one(c, "SELECT * FROM exchange_results WHERE principal_id=:p AND idem_key=:k", p=owner, k=idem)
            if prev:
                if prev["request_hash"] != request_hash:
                    raise GatewayError("idempotency_conflict", 409)
                if prev["expires_at"] <= utcnow() or not prev["ciphertext"]:
                    raise GatewayError("idempotency_result_expired", 409)
                return json.loads(self.store.open(prev["ciphertext"], f"exchange:{owner}:{idem}"))
            if self.s.provision_inference_on_login:
                await self.broker.ensure(owner)
            else:
                # Corporate MCP login need not allocate a paid inference key.
                async with self.db.tx() as c:
                    policy = await one(c, "SELECT providers FROM policies WHERE id=:id", id=p["policy_id"])
                if self.s.provider_mode not in policy["providers"]:
                    raise GatewayError("provider_not_allowed", 403)
            ident = uid("gc_")
            key = "cg_" + ident + "." + secrets.token_urlsafe(32)
            exp = utcnow() + timedelta(seconds=self.s.gateway_credential_ttl_seconds)
            response = {"credential": {"id": ident, "key": key, "expires_at": exp.isoformat()},
                        "provider": "company-ai", "base_url": self.s.gateway_public_url + "/v1",
                        "principal": {"id": owner}}
            cipher = self.store.seal(json.dumps(response), f"exchange:{owner}:{idem}")
            async with self.db.tx() as c:
                state = await one(c, "SELECT status,authz_allowed FROM principals WHERE id=:p FOR UPDATE", p=owner)
                if state["status"] != "active" or not state["authz_allowed"]:
                    raise GatewayError("principal_suspended", 403)
                await run(c, """INSERT INTO gateway_credentials(id,principal_id,secret_hash,device_name,expires_at)
                    VALUES (:id,:p,:h,:n,:exp)""", id=ident, p=owner,
                    h=digest(key, self.s.credential_hash_key.get_secret_value()), n=device, exp=exp)
                await run(c, """INSERT INTO exchange_results(principal_id,idem_key,request_hash,ciphertext,expires_at)
                    VALUES (:p,:k,:r,:cipher,:exp)""", p=owner, k=idem, r=request_hash, cipher=cipher,
                    exp=utcnow() + timedelta(minutes=5))
                await audit(c, owner, "credential.issue", ident, "success", idem)
            return response

    async def authenticate(self, key: str):
        if not key.startswith("cg_gc_") or len(key) > 160 or "." not in key:
            raise GatewayError("gateway_credential_invalid", 401)
        ident = key[3:].split(".", 1)[0]
        async with self.db.tx() as c:
            row = await one(c, AUTH_QUERY, id=ident)
        if not row:
            raise GatewayError("gateway_credential_invalid", 401)
        cred, p, policy = ({field: row[f"{table}_{field}"] for field in fields} for table, fields in AUTH_FIELDS.items())
        if not hmac.compare_digest(cred["secret_hash"], digest(key, self.s.credential_hash_key.get_secret_value())):
            raise GatewayError("gateway_credential_invalid", 401)
        if cred["revoked_at"] or cred["expires_at"] <= utcnow():
            raise GatewayError("gateway_credential_expired_or_revoked_relogin", 401)
        await self.check_status(p)
        return p, cred, policy

    async def suspend(self, owner: str, correlation: str):
        async with self.db.tx() as c:
            p = await one(c, "UPDATE principals SET status='suspended' WHERE id=:p RETURNING id", p=owner)
            if not p:
                raise GatewayError("principal_not_found", 404)
            await run(c, "UPDATE gateway_credentials SET revoked_at=COALESCE(revoked_at,now()) WHERE principal_id=:p", p=owner)
            for pc in await rows(c, "SELECT id FROM provider_credentials WHERE principal_id=:p AND state='active'", p=owner):
                await self.broker.enqueue_revoke(c, pc["id"])
            await audit(c, "admin", "principal.suspend", owner, "success", correlation)
