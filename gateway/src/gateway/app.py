import asyncio
import hmac
import json
import time
import ssl
from contextlib import asynccontextmanager, suppress
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth import Auth
from gateway.broker import Broker
from gateway.config import Settings
from gateway.db import DB, audit, one, rows
from gateway.errors import GatewayError
from gateway.inference import Chat, Inference, Usage, bounded_json, proxy_sse
from gateway.oidc import Identity
from gateway.secrets import AESSecretStore
from gateway.runtime import Runtime, RuntimeMiddleware
from providers.factory import providers


class ExchangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .@-]+$")
    client: Literal["kilo-cli", "opencode-cli"]


class BodyLimit:
    def __init__(self, app, limit, timeout=10):
        self.app, self.limit, self.timeout = app, limit, timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size, parts = 0, []
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    msg = await receive()
                    if msg["type"] == "http.disconnect":
                        return
                    size += len(msg.get("body", b""))
                    if size > self.limit:
                        return await JSONResponse({"error": {"code": "body_too_large"}}, status_code=413)(scope, receive, send)
                    parts.append(msg.get("body", b""))
                    if not msg.get("more_body"):
                        break
        except TimeoutError:
            return await JSONResponse({"error": {"code": "request_body_timeout"}}, status_code=408)(scope, receive, send)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(parts), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, identity=None, provider=None, worker=True):
    s = settings or Settings()
    db = DB(s.database_url.get_secret_value(), s.database_schema, pool_size=s.db_pool_size,
        max_overflow=s.db_max_overflow, pool_timeout=s.db_pool_timeout_seconds, statement_timeout_ms=s.db_statement_timeout_ms)
    runtime = Runtime(s.max_inflight_requests)
    tls = ssl.create_default_context(cafile=s.tls_ca_bundle) if s.tls_ca_bundle else True
    client = httpx.AsyncClient(timeout=httpx.Timeout(15, read=s.request_timeout_seconds, pool=s.http_pool_timeout_seconds),
                               limits=httpx.Limits(max_connections=s.http_max_connections,
                                   max_keepalive_connections=s.http_max_keepalive_connections, keepalive_expiry=30),
                               follow_redirects=False, trust_env=False,
                               verify=tls)
    identity_client = httpx.AsyncClient(timeout=s.identity_timeout_seconds, follow_redirects=False, trust_env=False,
        verify=tls, limits=httpx.Limits(max_connections=s.identity_max_connections,
                                       max_keepalive_connections=s.identity_max_connections, keepalive_expiry=30))
    store = AESSecretStore(s.encryption_key.get_secret_value(), s.encryption_key_version)
    ident = identity or Identity(s, identity_client)
    adapter, manager = (provider, provider) if provider else providers(s, client)
    broker = Broker(s, db, store, manager)
    auth = Auth(s, db, ident, broker, store)
    inference = Inference(s, db, broker)

    @asynccontextmanager
    async def lifespan(app):
        await auth.initialize()
        task = asyncio.create_task(broker.worker()) if worker and s.worker_enabled else None
        try:
            yield
        finally:
            runtime.draining = True
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await auth.close()
            await client.aclose()
            await identity_client.aclose()
            await db.engine.dispose()

    from urllib.parse import urlparse
    app = FastAPI(title="Y.AI Gateway", version="0.1.0", lifespan=lifespan,
                  root_path=urlparse(s.gateway_public_url).path.rstrip("/"),
                  docs_url="/docs" if s.profile == "dev" else None, redoc_url=None)
    app.state.db, app.state.broker, app.state.auth = db, broker, auth
    app.state.inference, app.state.identity = inference, ident
    app.state.runtime = runtime
    from prometheus_client import Gauge
    Gauge("yai_db_connections_checked_out", "Current checked-out DB pool connections", registry=runtime.registry).set_function(db.engine.pool.checkedout)
    app.add_middleware(BodyLimit, limit=s.body_limit, timeout=s.body_read_timeout_seconds)
    app.add_middleware(RuntimeMiddleware, runtime=runtime)

    @app.exception_handler(GatewayError)
    async def error(request, e):
        return JSONResponse({"error": {"code": e.code, "message": e.code}}, status_code=e.status,
                            headers={"Retry-After": str(e.retry_after)} if e.retry_after else {})

    @app.exception_handler(RequestValidationError)
    async def invalid(request, e):
        # Default validation errors can echo user messages and input credentials.
        return JSONResponse({"error": {"code": "invalid_request"}}, status_code=400)

    def bearer(authorization: str | None):
        if not authorization or not authorization.startswith("Bearer "):
            raise GatewayError("bearer_required", 401)
        return authorization[7:]

    async def authenticated(authorization: str | None = Header(default=None)):
        return await auth.authenticate(bearer(authorization))

    async def admin(authorization: str | None = Header(default=None)):
        token = bearer(authorization)
        if not hmac.compare_digest(token.encode(), s.admin_token.get_secret_value().encode()):
            raise GatewayError("admin_required", 403)

    from gateway.mcp import mount_mcp
    mount_mcp(app, s, db, auth, store, client, authenticated, bearer)

    @app.get("/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready():
        if runtime.draining:
            raise GatewayError("gateway_draining", 503)
        async with db.tx() as c:
            row = await one(c, """SELECT id,(SELECT count(*) FROM usage_daily WHERE false),
                (SELECT count(*) FROM mcp_credentials WHERE false) FROM policies WHERE id='default'""")
            if not row:
                raise GatewayError("policy_not_initialized")
        try:
            await ident.discover()
            if s.provider_mode == "corporate":
                raise GatewayError("provider_not_configured")
        except (httpx.HTTPError, KeyError, ValueError):
            raise GatewayError("identity_unavailable") from None
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        if not s.metrics_enabled:
            return Response(status_code=404)
        return Response(content=generate_latest(runtime.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/admin/drain", dependencies=[Depends(admin)])
    async def drain():
        runtime.draining = True
        return {"status": "draining", "inflight": runtime.active}

    @app.post("/auth/cli/exchange")
    async def exchange(body: ExchangeBody, authorization: str | None = Header(default=None),
                       idempotency_key: str = Header(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")):
        result = await auth.exchange(bearer(authorization), idempotency_key, body.device_name, body.client)
        return {**result, "mcp_setup_required": bool(s.jira_base_url or s.confluence_base_url)}

    @app.get("/auth/cli/credentials")
    async def credentials(ctx=Depends(authenticated)):
        async with db.tx() as c:
            data = await rows(c, """SELECT id,device_name,created_at,expires_at,revoked_at FROM gateway_credentials
                WHERE principal_id=:p ORDER BY created_at DESC""", p=ctx[0]["id"])
        return {"data": [dict(x) for x in data]}

    async def revoke_id(owner, ident, correlation):
        async with db.tx() as c:
            row = await one(c, """UPDATE gateway_credentials SET revoked_at=COALESCE(revoked_at,now())
                WHERE id=:id AND principal_id=:p RETURNING id""", id=ident, p=owner)
            if not row:
                raise GatewayError("credential_not_found", 404)
            await audit(c, owner, "credential.revoke", ident, "success", correlation)
        return Response(status_code=204)

    @app.delete("/auth/cli/credentials/current", status_code=204)
    async def revoke_current(request: Request, ctx=Depends(authenticated)):
        return await revoke_id(ctx[0]["id"], ctx[1]["id"], request.state.request_id)

    @app.delete("/auth/cli/credentials/{credential_id}", status_code=204)
    async def revoke(credential_id: str, request: Request, ctx=Depends(authenticated)):
        return await revoke_id(ctx[0]["id"], credential_id, request.state.request_id)

    @app.get("/me")
    async def me(ctx=Depends(authenticated)):
        p, gc, policy = ctx
        usage = {"charged_units": p["usage_charged_units"], "requests": p["usage_requests"]}
        return {"principal": {"id": p["id"], "status": p["status"]},
                "credential": {"id": gc["id"], "expires_at": gc["expires_at"]},
                "permissions": {"models": [m.id for m in s.allowed_models(policy)], "providers": policy["providers"]},
                "usage": dict(usage)}

    @app.get("/v1/models")
    async def models(ctx=Depends(authenticated)):
        if s.provider_mode == "corporate":
            raise GatewayError("provider_not_configured")
        return {"object": "list", "data": [{"id": m.id, "name": m.name, "object": "model", "owned_by": "y-ai",
                    "context_length": m.context_length, "max_output_tokens": m.max_output_tokens,
                    "supported_parameters": ["tools", "tool_choice"] if m.tools else []}
                    for m in s.allowed_models(ctx[2])]}

    @app.post("/v1/chat/completions")
    async def chat(body: Chat, request: Request, ctx=Depends(authenticated)):
        p, gc, policy = ctx
        payload, units = inference.prepare(body, policy)
        secret = await broker.inference_secret(p["id"])
        rid, usage = request.state.request_id, Usage()
        await inference.reserve(p["id"], gc["id"], body.model, units, rid)
        deadline = time.monotonic() + s.request_timeout_seconds
        upstream = None

        async def open_response():
            return await adapter.chat(payload, secret)

        async def watch_disconnect(stopped):
            while not stopped.is_set():
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.1)

        async def finish(outcome):
            # On disconnect, persist reservation/outcome independently of cancellation.
            task = asyncio.create_task(inference.finish(rid, usage, outcome))
            await asyncio.shield(task)

        try:
            stopped = asyncio.Event()
            task, watcher = asyncio.create_task(open_response()), asyncio.create_task(watch_disconnect(stopped))
            try:
                async with asyncio.timeout_at(deadline):
                    done, _ = await asyncio.wait([task, watcher], return_when=asyncio.FIRST_COMPLETED)
                    if watcher in done:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                        raise GatewayError("client_disconnected", 499)
                    upstream = await task
            finally:
                stopped.set()
                watcher.cancel()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, watcher, return_exceptions=True)
            if not body.stream:
                async with asyncio.timeout_at(deadline):
                    async def read_json():
                        return await bounded_json(upstream)
                    read_task = asyncio.create_task(read_json())
                    stopped = asyncio.Event()
                    watcher = asyncio.create_task(watch_disconnect(stopped))
                    try:
                        done, _ = await asyncio.wait([read_task, watcher], return_when=asyncio.FIRST_COMPLETED)
                        if watcher in done:
                            raise GatewayError("client_disconnected", 499)
                        data = await read_task
                    finally:
                        stopped.set()
                        read_task.cancel()
                        watcher.cancel()
                        await asyncio.gather(read_task, watcher, return_exceptions=True)
                usage.observe(data)
                if usage.error:
                    raise GatewayError("provider_response_error", 502)
                data["model"] = body.model
                await upstream.aclose()
                await finish("success")
                return JSONResponse(data)
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                raise GatewayError("provider_stream_content_type", 502)
        except BaseException as e:
            if upstream:
                await upstream.aclose()
            await finish(e.code if isinstance(e, GatewayError) else "provider_unavailable")
            if isinstance(e, (TimeoutError, httpx.HTTPError)):
                raise GatewayError("provider_timeout", 504) from None
            raise

        async def stream():
            outcome = "disconnected"
            try:
                async with asyncio.timeout_at(deadline):
                    async for frame in proxy_sse(upstream, usage, body.model):
                        yield frame
                outcome = "success"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                outcome = e.code if isinstance(e, GatewayError) else "provider_stream_interrupted"
                yield ("data: " + json.dumps({"error": {"code": outcome, "message": outcome},
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]}) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
            finally:
                await upstream.aclose()
                await finish(outcome)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/admin/users/{principal_id}/suspend", dependencies=[Depends(admin)])
    async def suspend(principal_id: str, request: Request):
        await auth.suspend(principal_id, request.state.request_id)
        return {"status": "suspended", "upstream_revoke": "queued"}

    @app.post("/admin/provider-credentials/{credential_id}/rotate", dependencies=[Depends(admin)])
    async def rotate(credential_id: str):
        async with db.tx() as c:
            row = await one(c, "SELECT * FROM provider_credentials WHERE id=:id AND state='active'", id=credential_id)
        if not row:
            raise GatewayError("credential_not_found", 404)
        if row["provider"] != s.credential_provider:
            raise GatewayError("credential_provider_mismatch", 409)
        new = await broker.ensure(row["principal_id"], rotate=True)
        return {"id": new["id"], "generation": new["generation"], "previous_revoke": "queued"}

    def documented_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "KeycloakAccessToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                "description": "Keycloak access token: RS256, canonical issuer, code-ai-api audience, kilo-cli azp and both roles. ID token rejected."},
            "GatewayCredential": {"type": "http", "scheme": "bearer",
                "description": "Opaque personal cg_gc_ credential. Expiry requires another CLI login."},
            "AdminCredential": {"type": "http", "scheme": "bearer",
                "description": "Independent server admin token. Never issued to the Kilo plugin."},
        }
        for path, operations in schema["paths"].items():
            scheme = "AdminCredential" if path.startswith("/admin/") else (
                "KeycloakAccessToken" if path == "/auth/cli/exchange" else "GatewayCredential")
            for operation in operations.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                if not path.startswith("/health/"):
                    operation["security"] = [{scheme: []}]
                for code, desc in (("400","Invalid request"),("401","Missing, expired or revoked credential"),
                                   ("403","Insufficient permissions"),("409","Idempotency or lifecycle conflict"),
                                   ("429","Principal/provider limit; Retry-After header"),("503","Dependency not available; fail closed")):
                    operation["responses"][code] = {"description":desc,
                        "content":{"application/json":{"schema":{"type":"object","properties":{
                            "error":{"type":"object","properties":{"code":{"type":"string"},"message":{"type":"string"}}}}}}}}
        schema["paths"]["/v1/chat/completions"]["post"]["responses"]["200"] = {
            "description": "OpenAI Chat Completions JSON or SSE. Errors after headers are safe SSE error events, never a changed status.",
            "content": {"application/json":{"schema":{"type":"object"}}, "text/event-stream":{"schema":{"type":"string"}}}}
        schema["paths"]["/auth/cli/exchange"]["post"]["responses"]["200"] = {
            "description":"Secret returned once, or same encrypted result for 5-minute idempotency replay. Cache-Control: no-store.",
            "content":{"application/json":{"schema":{"type":"object","required":["credential","provider","base_url","principal"],
                "properties":{"credential":{"type":"object","required":["id","key","expires_at"],"properties":{
                    "id":{"type":"string"},"key":{"type":"string","readOnly":True},"expires_at":{"type":"string","format":"date-time"}}},
                    "provider":{"const":"company-ai"},"base_url":{"type":"string","format":"uri"},
                    "principal":{"type":"object","properties":{"id":{"type":"string"}}}}}}}}
        app.openapi_schema = schema
        return schema
    app.openapi = documented_openapi
    return app
