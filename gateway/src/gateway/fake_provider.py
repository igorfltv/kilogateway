"""Development-only provider. Synthetic output is intentionally labeled FAKE."""
import asyncio
import json
import os
import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="FAKE development provider")
state_file = Path(os.environ.get("FAKE_STATE_FILE", "/tmp/company-ai-fake-keys.json"))
keys = json.loads(state_file.read_text()) if state_file.exists() else {}
controls = {"create_drop": False, "chat_status": 200, "midstream": False, "delay": 0.05,
            "created": 0, "revoked": 0, "cancelled": 0, "requests": 0}


def key_secret(ref):
    return "fake_" + hmac.new(os.environ["FAKE_MANAGEMENT_KEY"].encode(), ref.encode(), hashlib.sha256).hexdigest()


def persist():
    temp = state_file.with_suffix(".tmp")
    temp.write_text(json.dumps(keys))
    temp.replace(state_file)


@app.middleware("http")
async def guard(request, call_next):
    expected = os.environ.get("FAKE_MANAGEMENT_KEY", "")
    if request.url.path.startswith(("/v1/keys", "/control")):
        if not expected or request.headers.get("authorization") != "Bearer " + expected:
            return JSONResponse({}, status_code=403)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "FAKE"}


@app.post("/v1/keys", status_code=201)
async def create(body: dict):
    ref = uuid4().hex
    keys[ref] = {"hash": ref, "name": body["name"], "limit": body["limit"],
                 "expires_at": body["expires_at"], "disabled": False, "usage": 0}
    persist()
    controls["created"] += 1
    if controls["create_drop"]:
        # Simulate provider-side success with a lost usable create response.
        return Response(status_code=504)
    return {"data": keys[ref], "key": key_secret(ref)}


@app.get("/v1/keys")
async def listing(offset: int = 0):
    return {"data": [{k: v for k, v in x.items() if k != "secret"} for x in list(keys.values())[offset:offset+100]]}


@app.get("/v1/keys/{ref}")
async def inspect(ref: str):
    if ref not in keys:
        return Response(status_code=404)
    return {"data": {k: v for k, v in keys[ref].items() if k != "secret"}}


@app.delete("/v1/keys/{ref}", status_code=204)
async def delete(ref: str):
    if keys.pop(ref, None):
        controls["revoked"] += 1
        persist()
    return Response(status_code=204)


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "fake/tool-coder", "context_length": 262144,
                      "top_provider": {"max_completion_tokens": 4096}, "supported_parameters": ["tools", "tool_choice"]}]}


@app.post("/v1/chat/completions")
async def chat(body: dict, request: Request, authorization: str = Header(default="")):
    key = next((v for ref, v in keys.items() if authorization == "Bearer " + key_secret(ref)), None)
    if not key or key["disabled"] or datetime.fromisoformat(key["expires_at"]) <= datetime.now(timezone.utc):
        return JSONResponse({"error": {"message": "fake rejected"}}, status_code=401)
    controls["requests"] += 1
    if controls["chat_status"] != 200:
        return JSONResponse({"error": {"message": "not forwarded"}}, status_code=controls["chat_status"])
    tools = body.get("tools")
    tool_result = any(x["role"] == "tool" for x in body["messages"])
    force = body.get("tool_choice")
    tool_mode = bool(tools and not tool_result and (force == "required" or isinstance(force, dict)))
    name = tools[0]["function"]["name"] if tool_mode else ""
    message = {"role": "assistant", "content": "FAKE: local gateway round trip OK"}
    reason = "stop"
    if tool_mode:
        reason = "tool_calls"
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_fake", "type": "function", "function": {"name": name, "arguments": '{"value":"FAKE"}'}}]}
    usage = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "cost": 0}
    if not body.get("stream"):
        return {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                "model": body["model"], "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": usage}

    async def generate():
        completed = False
        try:
            if tool_mode:
                deltas = [{"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fake", "type": "function",
                            "function": {"name": name, "arguments": ""}}]},
                          {"tool_calls": [{"index": 0, "function": {"arguments": '{"value":'}}]},
                          {"tool_calls": [{"index": 0, "function": {"arguments": '"FAKE"}'}}]}]
            else:
                deltas = [{"role": "assistant", "content": "FAKE: "}, {"content": "local gateway "}, {"content": "round trip OK"}]
            for index, delta in enumerate(deltas):
                frame = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                yield "data: " + json.dumps(frame) + "\n\n"
                await asyncio.sleep(controls["delay"])
                if controls["midstream"] and index == 0:
                    yield 'data: {"error":{"message":"not forwarded"}}\n\n'
                    return
            yield "data: " + json.dumps({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": int(time.time()),
                  "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": reason}], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
            completed = True
        finally:
            if not completed:
                controls["cancelled"] += 1
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/control")
async def state():
    return {**controls, "active_keys": len(keys)}


@app.post("/control")
async def control(body: dict):
    for k in ("create_drop", "chat_status", "midstream", "delay"):
        if k in body:
            controls[k] = body[k]
    return {"ok": True}
