import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from gateway.db import audit, one, rows, run
from gateway.errors import GatewayError


def uid(prefix: str):
    return prefix + uuid4().hex


def utcnow():
    return datetime.now(timezone.utc)


class Broker:
    def __init__(self, settings, db, secrets, provider):
        self.s, self.db, self.secrets, self.provider = settings, db, secrets, provider

    @asynccontextmanager
    async def lock(self, owner: str):
        # PostgreSQL lock across replicas. Durable state is committed on separate connections before I/O.
        async with self.db.tx() as conn:
            await run(conn, "SET LOCAL lock_timeout = '15s'")
            await run(conn, "SELECT pg_advisory_xact_lock(hashtextextended(:id,0))", id=owner)
            yield

    async def active(self, owner: str):
        async with self.db.tx() as c:
            return await one(c, """SELECT * FROM provider_credentials WHERE principal_id=:p
                      AND provider=:v AND state='active'""", p=owner, v=self.s.credential_provider)

    async def enqueue_revoke(self, c, credential_id: str):
        await run(c, "UPDATE provider_credentials SET state='revoke_pending' WHERE id=:id AND state='active'",
                  id=credential_id)
        await run(c, "INSERT INTO durable_jobs(kind,credential_id) VALUES ('revoke',:id) ON CONFLICT DO NOTHING",
                  id=credential_id)

    async def ensure(self, owner: str, rotate: bool = False):
        if self.s.shared_provider_credential:
            if rotate:
                raise GatewayError("provider_rotation_not_supported", 409)
            # No invented /keys API, per-user upstream credential or monetary allocation.
            # The gateway still enforces personal access, rate and token limits.
            async with self.db.tx() as c:
                principal = await one(c, "SELECT * FROM principals WHERE id=:p", p=owner)
                if not principal or principal["status"] != "active" or not principal["authz_allowed"]:
                    raise GatewayError("principal_suspended", 403)
                policy = await one(c, "SELECT providers FROM policies WHERE id=:id", id=principal["policy_id"])
                if self.s.provider_mode not in policy["providers"]:
                    raise GatewayError("provider_not_allowed", 403)
            return None
        if not self.provider.capabilities().managed_keys:
            raise GatewayError("provider_not_configured")
        async with self.lock(owner + ":provider"):
            old = await self.active(owner)
            if old and old["expires_at"] > utcnow() and not rotate:
                status = await self.provider.inspect(old["provider_reference"])
                if status["disabled"]:
                    raise GatewayError("provider_key_rejected", 502)
                return old
            async with self.db.tx() as c:
                p = await one(c, "SELECT * FROM principals WHERE id=:p FOR UPDATE", p=owner)
                if not p or p["status"] != "active" or not p["authz_allowed"]:
                    raise GatewayError("principal_suspended", 403)
                pending = await one(c, """SELECT id FROM provisioning_operations WHERE principal_id=:p
                    AND provider=:v AND state IN ('started','uncertain')""", p=owner, v=self.s.credential_provider)
                if pending:
                    raise GatewayError("provisioning_reconciliation_required")
                if rotate and await one(c, """SELECT id FROM usage_records WHERE principal_id=:p
                    AND outcome='in_flight' AND lease_expires_at>now() LIMIT 1""", p=owner):
                    raise GatewayError("rotation_wait_for_requests", 409)
                pol = await one(c, "SELECT * FROM policies WHERE id=:id", id=p["policy_id"])
                if self.s.provider_mode not in pol["providers"]:
                    raise GatewayError("provider_not_allowed", 403)
                amount = self.s.openrouter_test_key_limit_usd
                if float(p["provider_allocated"]) + amount > float(pol["provider_budget"]) + 1e-9:
                    raise GatewayError("principal_provider_budget_exhausted", 429, 3600)
                op = uid("op_")
                label = "company-ai:" + op
                await run(c, """INSERT INTO provisioning_operations(id,principal_id,provider,label,state)
                    VALUES (:id,:p,:v,:label,'started')""", id=op, p=owner, v=self.s.credential_provider, label=label)
                await run(c, "UPDATE principals SET provider_allocated=provider_allocated+:n WHERE id=:p",
                          p=owner, n=amount)
                await audit(c, owner, "provider.create", op, "started", op)
            expires = utcnow() + timedelta(seconds=self.s.provider_key_ttl_seconds)
            try:
                created = await self.provider.provision(label, amount, expires.isoformat())
            except BaseException:
                # Even an error response may be ambiguous. Never retry create automatically.
                async with self.db.tx() as c:
                    await run(c, "UPDATE provisioning_operations SET state='uncertain',updated_at=now() WHERE id=:id",
                              id=op)
                    await audit(c, owner, "provider.create", op, "uncertain", op)
                raise GatewayError("provisioning_reconciliation_required") from None
            ident = uid("pc_")
            try:
                ciphertext = self.secrets.seal(created.secret, f"provider:{owner}:{self.s.credential_provider}:{ident}")
                async with self.db.tx() as c:
                    p = await one(c, "SELECT status,authz_allowed FROM principals WHERE id=:p FOR UPDATE", p=owner)
                    if p["status"] != "active" or not p["authz_allowed"]:
                        raise GatewayError("principal_suspended", 403)
                    gen = await one(c, "SELECT COALESCE(max(generation),0)+1 AS n FROM provider_credentials WHERE principal_id=:p AND provider=:v",
                                    p=owner, v=self.s.credential_provider)
                    if old:
                        await self.enqueue_revoke(c, old["id"])
                    await run(c, """INSERT INTO provider_credentials
                        (id,principal_id,provider,provider_reference,label,ciphertext,state,generation,expires_at,allocation)
                        VALUES (:id,:p,:v,:r,:l,:cipher,'active',:g,:exp,:a)""", id=ident, p=owner,
                        v=self.s.credential_provider, r=created.reference, l=label, cipher=ciphertext,
                        g=gen["n"], exp=expires, a=amount)
                    await run(c, "UPDATE provisioning_operations SET state='complete',updated_at=now() WHERE id=:id", id=op)
                    await audit(c, owner, "provider.activate", ident, "success", op)
            except BaseException:
                # The committed operation is recovery evidence even if DB is unavailable during compensation.
                try:
                    await self.provider.revoke(created.reference)
                    async with self.db.tx() as c:
                        await run(c, "UPDATE provisioning_operations SET state='failed',updated_at=now() WHERE id=:id", id=op)
                except BaseException:
                    pass
                raise GatewayError("provider_persistence_failed") from None
            return await self.active(owner)

    def secret(self, credential):
        return self.secrets.open(credential["ciphertext"],
             f"provider:{credential['principal_id']}:{credential['provider']}:{credential['id']}")

    async def inference_secret(self, owner):
        if self.s.shared_provider_credential:
            return self.s.provider_api_key.get_secret_value() if self.s.provider_credential_mode == "static" else ""
        credential = await self.active(owner)
        if not credential:
            raise GatewayError("provider_credential_unavailable")
        if credential["expires_at"] <= utcnow():
            raise GatewayError("provider_credential_expired_relogin", 503)
        return self.secret(credential)

    async def tick(self):
        # A singleton job pass is enforced by PostgreSQL, including across replicas.
        async with self.db.tx() as guard:
            got = await one(guard, "SELECT pg_try_advisory_xact_lock(91472611) AS ok")
            if not got["ok"]:
                return
            async with self.db.tx() as c:
                await run(c, "UPDATE exchange_results SET ciphertext=NULL WHERE expires_at<=now() AND ciphertext IS NOT NULL")
                await run(c, "DELETE FROM rate_windows WHERE minute < extract(epoch from now())::bigint/60-120")
                await run(c, """UPDATE usage_records SET outcome='lease_expired',finished_at=now()
                    WHERE outcome='in_flight' AND lease_expires_at<=now()""")
                jobs = await rows(c, """SELECT j.*,p.provider_reference,p.principal_id,p.provider
                    FROM durable_jobs j JOIN provider_credentials p ON p.id=j.credential_id
                    WHERE j.state='pending' AND j.retry_at<=now() ORDER BY j.id LIMIT 20""")
            for j in jobs:
                if j["provider"] != self.s.credential_provider or self.provider is None:
                    continue
                try:
                    # Active old-generation requests get a grace period bounded by their lease.
                    async with self.db.tx() as c:
                        live = await one(c, """SELECT id FROM usage_records WHERE principal_id=:p
                          AND outcome='in_flight' AND lease_expires_at>now() LIMIT 1""", p=j["principal_id"])
                    if live:
                        continue
                    await self.provider.revoke(j["provider_reference"])
                    async with self.db.tx() as c:
                        await run(c, "UPDATE provider_credentials SET state='revoked',ciphertext=NULL WHERE id=:id", id=j["credential_id"])
                        await run(c, "UPDATE durable_jobs SET state='done',attempts=attempts+1 WHERE id=:id", id=j["id"])
                        await audit(c, "worker", "provider.revoke", j["credential_id"], "success", str(j["id"]))
                except Exception:
                    async with self.db.tx() as c:
                        await run(c, """UPDATE durable_jobs SET attempts=attempts+1,
                            retry_at=now()+make_interval(secs=>:delay) WHERE id=:id""",
                            id=j["id"], delay=min(3600, 2 ** min(j["attempts"] + 1, 11)))
                        await audit(c, "worker", "provider.revoke", j["credential_id"], "retry", str(j["id"]))
            async with self.db.tx() as c:
                ops = await rows(c, """SELECT * FROM provisioning_operations WHERE state IN ('started','uncertain')
                    AND updated_at<now()-interval '30 seconds' AND provider=:v LIMIT 20""", v=self.s.credential_provider)
            for op in ops:
                async with self.lock(op["principal_id"] + ":provider"):
                    async with self.db.tx() as c:
                        fresh = await one(c, "SELECT state FROM provisioning_operations WHERE id=:id", id=op["id"])
                    if fresh["state"] not in {"started", "uncertain"}:
                        continue
                    try:
                        refs = await self.provider.find(op["label"])
                        for ref in refs:
                            await self.provider.revoke(ref)
                        # No match cannot prove that a timed-out create did not execute. Keep quarantined.
                        if refs:
                            async with self.db.tx() as c:
                                await run(c, "UPDATE provisioning_operations SET state='failed',updated_at=now() WHERE id=:id", id=op["id"])
                                await audit(c, "worker", "provider.reconcile", op["id"], "orphans_revoked", op["id"])
                    except Exception:
                        pass

    async def worker(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # No exception interpolation; DB/provider errors may contain sensitive parameters.
                import logging
                logging.getLogger("gateway").warning("durable_worker_pass_failed")
            await asyncio.sleep(self.s.worker_interval_seconds)
