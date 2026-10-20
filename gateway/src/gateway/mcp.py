"""User-scoped PAT onboarding and stateless Streamable HTTP MCP endpoints."""
import asyncio
import hmac
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag

from fastapi import Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gateway.atlassian import TOOLS, Atlassian
from gateway.broker import uid, utcnow
from gateway.db import audit, one, rows, run
from gateway.errors import GatewayError
from gateway.secrets import digest

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


class PATChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    action: str = Field(pattern="^(keep|replace|remove)$")
    token: SecretStr = SecretStr("")


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    jira: PATChange = Field(default_factory=lambda: PATChange(action="keep"))
    confluence: PATChange = Field(default_factory=lambda: PATChange(action="keep"))


class SetupStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    ticket: SecretStr


class MCP:
    def __init__(self, settings, db, auth, store, client):
        self.s, self.db, self.auth, self.store = settings, db, auth, store
        self.atlassian = Atlassian(settings, client)

    def binding(self, owner, system):
        return f"mcp:{owner}:{system}:{self.atlassian.endpoint(system)}"

    async def catalog(self, owner):
        async with self.db.tx() as c:
            configured = {r["system"]: r["endpoint"] for r in await rows(c,
                "SELECT system,endpoint FROM mcp_credentials WHERE principal_id=:p", p=owner)}
        return {"servers": [{"id": system, "name": system.title(),
                "connected": configured.get(system) == self.atlassian.endpoint(system),
                "url": self.s.gateway_public_url + "/mcp/" + system}
                for system in TOOLS if self.atlassian.endpoint(system)]}

    async def session(self, ticket):
        if not ticket.startswith("ms_") or len(ticket) > 180 or "." not in ticket:
            raise GatewayError("mcp_setup_expired", 401)
        ident = ticket.split(".", 1)[0]
        async with self.db.tx() as c:
            row = await one(c, "SELECT * FROM mcp_setup_sessions WHERE id=:id", id=ident)
            gc = await one(c, "SELECT * FROM gateway_credentials WHERE id=:id", id=row["credential_id"]) if row else None
            p = await one(c, "SELECT * FROM principals WHERE id=:id", id=row["principal_id"]) if row else None
        if not row:
            raise GatewayError("mcp_setup_pending", 404)
        if (not row or not hmac.compare_digest(row["secret_hash"], digest(ticket, self.s.credential_hash_key.get_secret_value()))
                or row["expires_at"] <= utcnow() or row["completed_at"] or not gc or gc["revoked_at"]
                or gc["expires_at"] <= utcnow()):
            raise GatewayError("mcp_setup_expired", 401)
        await self.auth.check_status(p)
        return row

    async def reserve(self, owner, gcid):
        ident, minute = uid("mcpcall_"), int(utcnow().timestamp()) // 60
        async with self.db.tx() as c:
            p = await one(c, "SELECT status,authz_allowed FROM principals WHERE id=:p FOR UPDATE", p=owner)
            gc = await one(c, "SELECT revoked_at,expires_at FROM gateway_credentials WHERE id=:id FOR UPDATE", id=gcid)
            if p["status"] != "active" or not p["authz_allowed"] or gc["revoked_at"] or gc["expires_at"] <= utcnow():
                raise GatewayError("gateway_credential_expired_or_revoked_relogin", 401)
            await run(c, "DELETE FROM mcp_call_leases WHERE principal_id=:p AND expires_at<=now()", p=owner)
            active = await one(c, "SELECT count(*) AS n FROM mcp_call_leases WHERE principal_id=:p", p=owner)
            rate = await one(c, "SELECT count FROM mcp_rate_windows WHERE principal_id=:p AND minute=:m", p=owner, m=minute)
            if active["n"] >= self.s.mcp_request_concurrency:
                raise GatewayError("mcp_concurrency_limited", 429, 2)
            if rate and rate["count"] >= self.s.mcp_request_rate:
                raise GatewayError("mcp_rate_limited", 429, 60)
            await run(c, "DELETE FROM mcp_rate_windows WHERE principal_id=:p AND minute<:m", p=owner, m=minute)
            await run(c, """INSERT INTO mcp_rate_windows(principal_id,minute,count) VALUES (:p,:m,1)
                ON CONFLICT(principal_id,minute) DO UPDATE SET count=mcp_rate_windows.count+1""", p=owner, m=minute)
            await run(c, "INSERT INTO mcp_call_leases(id,principal_id,expires_at) VALUES (:id,:p,:exp)",
                      id=ident, p=owner, exp=utcnow() + timedelta(seconds=self.s.mcp_timeout_seconds + 5))
        return ident

    async def call(self, owner, gcid, system, name, arguments, correlation):
        if not isinstance(name, str) or name not in {t["name"] for t in TOOLS[system]}:
            raise GatewayError("mcp_tool_not_found", 404)
        lease = await self.reserve(owner, gcid)
        outcome = "failed"
        try:
            async with self.db.tx() as c:
                row = await one(c, "SELECT * FROM mcp_credentials WHERE principal_id=:p AND system=:s", p=owner, s=system)
            if not row or row["endpoint"] != self.atlassian.endpoint(system):
                raise GatewayError("mcp_pat_required_relogin", 403)
            token = self.store.open(row["ciphertext"], self.binding(owner, system))
            async with asyncio.timeout(self.s.mcp_timeout_seconds):
                result = await self.atlassian.call(system, name, arguments, token)
            # Defense in depth for upstreams accidentally echoing Authorization values.
            def redact(value):
                if isinstance(value, str):
                    return value.replace(token, "[REDACTED]")
                if isinstance(value, list):
                    return [redact(item) for item in value]
                if isinstance(value, dict):
                    return {redact(k): redact(v) for k, v in value.items()}
                return value
            text = json.dumps(redact(result), ensure_ascii=False)
            outcome = "success"
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except TimeoutError:
            raise GatewayError("mcp_upstream_unavailable", 502) from None
        except (ValueError, KeyError, TypeError, AttributeError, InvalidTag):
            raise GatewayError("mcp_upstream_unavailable", 502) from None
        finally:
            async def finish():
                async with self.db.tx() as c:
                    await run(c, "DELETE FROM mcp_call_leases WHERE id=:id", id=lease)
                    await audit(c, owner, "mcp.tool.call", system + ":" + name, outcome, correlation)
            await asyncio.shield(finish())


def mount_mcp(app, s, db, auth, store, client, authenticated, bearer):
    mcp = MCP(s, db, auth, store, client)
    app.state.mcp = mcp
    parsed = urlsplit(s.gateway_public_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    def check_origin(request, required=False):
        supplied = request.headers.get("origin")
        if (required and not supplied) or (supplied is not None and supplied != origin):
            raise GatewayError("mcp_origin_denied", 403)

    @app.get("/mcp/catalog")
    async def catalog(ctx=Depends(authenticated)):
        return JSONResponse(await mcp.catalog(ctx[0]["id"]), headers=NO_STORE)

    @app.get("/auth/cli/options")
    async def options():
        return JSONResponse({"mcp_setup": bool(s.jira_base_url or s.confluence_base_url)}, headers=NO_STORE)

    @app.post("/auth/mcp/setup")
    async def start(request: Request, body: SetupStartBody | None = None, ctx=Depends(authenticated)):
        check_origin(request)
        owner, gc = ctx[0]["id"], ctx[1]
        ticket = body.ticket.get_secret_value() if body else uid("ms_") + "." + secrets.token_urlsafe(32)
        if not re.fullmatch(r"ms_[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{32,86}", ticket):
            raise GatewayError("mcp_setup_invalid", 400)
        ident = ticket.split(".", 1)[0]
        exp = min(gc["expires_at"], utcnow() + timedelta(seconds=s.mcp_setup_ttl_seconds))
        async with db.tx() as c:
            # Serializes setup creation and invalidates previous pending tickets for this device.
            await one(c, "SELECT id FROM gateway_credentials WHERE id=:id FOR UPDATE", id=gc["id"])
            if await one(c, "SELECT id FROM mcp_setup_sessions WHERE id=:id", id=ident):
                raise GatewayError("mcp_setup_conflict", 409)
            await run(c, "DELETE FROM mcp_setup_sessions WHERE credential_id=:id OR expires_at<=now()", id=gc["id"])
            await run(c, """INSERT INTO mcp_setup_sessions(id,principal_id,credential_id,secret_hash,expires_at)
                VALUES (:id,:p,:gc,:hash,:exp)""", id=ident, p=owner, gc=gc["id"],
                hash=digest(ticket, s.credential_hash_key.get_secret_value()), exp=exp)
        return JSONResponse({"id": ident, "url": s.gateway_public_url + "/auth/mcp/setup#" + ticket,
                             "expires_at": exp.isoformat()}, headers=NO_STORE)

    @app.get("/auth/mcp/setup/status/{session_id}")
    async def poll(session_id: str, ctx=Depends(authenticated)):
        async with db.tx() as c:
            row = await one(c, """SELECT completed_at,expires_at FROM mcp_setup_sessions
                WHERE id=:id AND principal_id=:p AND credential_id=:gc""", id=session_id,
                p=ctx[0]["id"], gc=ctx[1]["id"])
        if not row or row["expires_at"] <= utcnow():
            raise GatewayError("mcp_setup_expired", 410)
        return JSONResponse({"complete": bool(row["completed_at"])}, headers=NO_STORE)

    @app.get("/auth/mcp/setup")
    async def page():
        return HTMLResponse((Path(__file__).parent / "mcp_setup.html").read_text(), headers={**NO_STORE,
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY"})

    @app.get("/auth/mcp/setup.js", include_in_schema=False)
    async def script():
        return Response((Path(__file__).parent / "mcp_setup.js").read_text(), media_type="text/javascript", headers=NO_STORE)

    @app.get("/auth/mcp/setup/session")
    async def setup_info(request: Request, authorization: str | None = Header(default=None)):
        check_origin(request)
        row = await mcp.session(bearer(authorization))
        result = await mcp.catalog(row["principal_id"])
        result["expires_at"] = row["expires_at"].isoformat()
        return JSONResponse(result, headers=NO_STORE)

    @app.post("/auth/mcp/setup/session")
    async def save(body: SetupBody, request: Request, authorization: str | None = Header(default=None)):
        check_origin(request, required=True)
        session = await mcp.session(bearer(authorization))
        changes = []
        for system in TOOLS:
            change = getattr(body, system)
            if change.action == "keep":
                if change.token.get_secret_value():
                    raise GatewayError("mcp_invalid_pat_action", 400)
                continue
            if not mcp.atlassian.endpoint(system):
                raise GatewayError("mcp_not_configured", 404)
            if change.action == "remove":
                changes.append((system, None))
                continue
            token = change.token.get_secret_value()
            if not 8 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise GatewayError("mcp_invalid_pat", 400)
            try:
                await mcp.atlassian.validate(system, token)
            except GatewayError as e:
                if e.code == "mcp_pat_rejected":
                    raise GatewayError("mcp_" + system + "_pat_rejected", 422) from None
                raise
            changes.append((system, store.seal(token, mcp.binding(session["principal_id"], system))))
        await mcp.session(bearer(authorization))
        async with db.tx() as c:
            # Recheck revocation and session consumption after network validation.
            p = await one(c, "SELECT status,authz_allowed FROM principals WHERE id=:p FOR UPDATE", p=session["principal_id"])
            gc = await one(c, "SELECT revoked_at,expires_at FROM gateway_credentials WHERE id=:id FOR UPDATE", id=session["credential_id"])
            current = await one(c, "SELECT * FROM mcp_setup_sessions WHERE id=:id FOR UPDATE", id=session["id"])
            if (not current or current["completed_at"] or current["expires_at"] <= utcnow() or gc["revoked_at"]
                    or gc["expires_at"] <= utcnow() or p["status"] != "active" or not p["authz_allowed"]):
                raise GatewayError("mcp_setup_expired", 401)
            for system, cipher in changes:
                if cipher is None:
                    await run(c, "DELETE FROM mcp_credentials WHERE principal_id=:p AND system=:s", p=session["principal_id"], s=system)
                else:
                    await run(c, """INSERT INTO mcp_credentials(principal_id,system,endpoint,ciphertext) VALUES (:p,:s,:e,:c)
                        ON CONFLICT(principal_id,system) DO UPDATE SET endpoint=:e,ciphertext=:c,updated_at=now()""",
                        p=session["principal_id"], s=system, e=mcp.atlassian.endpoint(system), c=cipher)
                await audit(c, session["principal_id"], "mcp.pat.remove" if cipher is None else "mcp.pat.save",
                            system, "success", request.state.request_id)
            await run(c, "UPDATE mcp_setup_sessions SET completed_at=now() WHERE id=:id", id=session["id"])
        return JSONResponse({"complete": True}, headers=NO_STORE)

    @app.delete("/mcp/credentials/{system}", status_code=204)
    async def remove(system: str, request: Request, ctx=Depends(authenticated)):
        if system not in TOOLS:
            raise GatewayError("mcp_not_configured", 404)
        async with db.tx() as c:
            await run(c, "DELETE FROM mcp_credentials WHERE principal_id=:p AND system=:s", p=ctx[0]["id"], s=system)
            await audit(c, ctx[0]["id"], "mcp.pat.remove", system, "success", request.state.request_id)
        return Response(status_code=204, headers=NO_STORE)

    @app.post("/mcp/{system}", operation_id="mcp_rpc")
    @app.api_route("/mcp/{system}", methods=["GET", "DELETE"], include_in_schema=False)
    async def rpc(system: str, request: Request, ctx=Depends(authenticated)):
        check_origin(request)
        if system not in TOOLS or not mcp.atlassian.endpoint(system):
            raise GatewayError("mcp_not_configured", 404)
        if request.method != "POST":
            return Response(status_code=405, headers={**NO_STORE, "Allow": "POST"})
        version = request.headers.get("mcp-protocol-version")
        if version and version not in VERSIONS:
            raise GatewayError("mcp_protocol_version_unsupported", 400)
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400, headers=NO_STORE)
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or
                not isinstance(message.get("method"), str) or
                ("id" in message and (type(message["id"]) not in {int, str} or len(str(message["id"])) > 128))):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}, status_code=400, headers=NO_STORE)
        if "id" not in message:
            return Response(status_code=202, headers=NO_STORE)
        method, params = message["method"], message.get("params", {})
        payload = {"jsonrpc": "2.0", "id": message["id"]}
        if not isinstance(params, dict):
            payload["error"] = {"code": -32602, "message": "Invalid params"}
        elif method == "initialize":
            requested = params.get("protocolVersion")
            payload["result"] = {"protocolVersion": requested if isinstance(requested, str) and requested in VERSIONS else "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "Y.AI " + system, "version": "0.1.0"}}
        elif method == "ping":
            payload["result"] = {}
        elif method == "tools/list":
            catalog = await mcp.catalog(ctx[0]["id"])
            connected = any(item["id"] == system and item["connected"] for item in catalog["servers"])
            payload["result"] = {"tools": TOOLS[system] if connected else []}
        elif method == "tools/call":
            try:
                payload["result"] = await mcp.call(ctx[0]["id"], ctx[1]["id"], system,
                    params.get("name"), params.get("arguments", {}), request.state.request_id)
            except GatewayError as e:
                payload["result"] = {"content": [{"type": "text", "text": e.code}], "isError": True}
        else:
            payload["error"] = {"code": -32601, "message": "Method not found"}
        return JSONResponse(payload, headers=NO_STORE)
