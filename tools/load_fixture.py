"""Loopback-only synthetic upstream/IdP for gateway capacity tests. Never deploy it."""
import asyncio
import json
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

app = FastAPI()
stats = {"chat_requests": 0, "active": 0, "peak_active": 0, "identity_checks": 0}


@app.get("/health")
async def health():
    return stats


@app.get("/realms/load/.well-known/openid-configuration")
async def discover():
    issuer = os.environ["LOAD_ISSUER"]
    return {"issuer": issuer, "token_endpoint": issuer + "/token", "jwks_uri": issuer + "/jwks"}


@app.post("/realms/load/token")
async def token():
    return {"access_token": "load-fixture-service", "expires_in": 300}


@app.get("/admin/realms/load/users/{subject}")
async def status(subject: str):
    stats["identity_checks"] += 1
    return {"enabled": True}


@app.get("/admin/realms/load/users/{subject}/role-mappings/realm/composite")
async def roles(subject: str):
    return [{"name": "invoke"}, {"name": "issue-credential"}]


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if request.headers.get("authorization") != "Bearer " + os.environ["LOAD_INFERENCE_KEY"]:
        return Response(status_code=401)
    body = await request.json()
    stats["chat_requests"] += 1
    stats["active"] += 1
    stats["peak_active"] = max(stats["peak_active"], stats["active"])
    delay = body.get("seed", 40) / 1000
    usage = {"prompt_tokens": 16, "completion_tokens": 1, "cost": 0}
    if not body.get("stream"):
        try:
            await asyncio.sleep(delay)
            return {"id": "synthetic", "object": "chat.completion", "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": usage}
        finally:
            stats["active"] -= 1

    async def stream():
        try:
            yield ("data: " + json.dumps({"model": body["model"], "choices": [{"index": 0, "delta": {"content": "OK"}}]}) + "\n\n").encode()
            await asyncio.sleep(delay)
            yield ("data: " + json.dumps({"model": body["model"], "choices": [], "usage": usage}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        finally:
            stats["active"] -= 1
    return StreamingResponse(stream(), media_type="text/event-stream")
