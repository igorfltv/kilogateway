import json
import math
import time
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gateway.broker import utcnow
from gateway.db import audit, one, run
from gateway.errors import GatewayError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Function(Strict):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    parameters: dict = Field(default_factory=dict)
    strict: bool | None = None


class Tool(Strict):
    type: Literal["function"]
    function: Function


class Message(Strict):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict] | None = None
    name: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None
    reasoning_details: list[dict] | None = None
    refusal: str | None = None


class Chat(Strict):
    model: str
    messages: list[Message] = Field(min_length=1, max_length=256)
    stream: bool = False
    stream_options: dict | None = None
    tools: list[Tool] | None = Field(default=None, max_length=64)
    tool_choice: str | dict | None = None
    parallel_tool_calls: bool | None = None
    max_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_completion_tokens: int | None = Field(default=None, ge=1, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    response_format: dict | None = None
    # OpenCode v2 sends OpenAI's stateless storage flag by default.
    store: Literal[False] | None = None


class Usage:
    def __init__(self):
        self.prompt, self.completion, self.cost = None, None, None
        self.error = False

    def observe(self, obj):
        if obj.get("error"):
            self.error = True
        u = obj.get("usage")
        if not isinstance(u, dict):
            return
        for attr, key in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
            val = u.get(key)
            if type(val) is int and 0 <= val < 10**9:
                setattr(self, attr, val)
        val = u.get("cost")
        if type(val) in (float, int) and math.isfinite(val) and 0 <= val < 10**6:
            self.cost = val


class Inference:
    def __init__(self, s, db, broker):
        self.s, self.db, self.broker = s, db, broker

    def prepare(self, chat: Chat, policy):
        if chat.model not in policy["aliases"] or self.s.provider_mode not in policy["providers"]:
            raise GatewayError("model_not_allowed", 403)
        model = next((m for m in self.s.models if m.id == chat.model), None)
        if not model:
            raise GatewayError("model_not_configured", 403)
        if (chat.tools or chat.tool_choice) and not model.tools:
            raise GatewayError("tools_not_supported", 400)
        if chat.max_tokens and chat.max_completion_tokens:
            raise GatewayError("conflicting_output_limits", 400)
        output = chat.max_tokens or chat.max_completion_tokens or model.max_output_tokens
        if output > model.max_output_tokens:
            raise GatewayError("output_limit_exceeded", 400)
        if chat.stream_options and set(chat.stream_options) - {"include_usage"}:
            raise GatewayError("unsupported_stream_options", 400)
        if chat.stream_options and type(chat.stream_options.get("include_usage")) is not bool:
            raise GatewayError("unsupported_stream_options", 400)
        if isinstance(chat.tool_choice, str) and chat.tool_choice not in {"auto", "none", "required"}:
            raise GatewayError("invalid_tool_choice", 400)
        if isinstance(chat.tool_choice, dict):
            choice = chat.tool_choice
            fn = choice.get("function")
            if set(choice) != {"type", "function"} or choice["type"] != "function" or not isinstance(fn, dict) or set(fn) != {"name"}:
                raise GatewayError("invalid_tool_choice", 400)
            if fn["name"] not in {t.function.name for t in chat.tools or []}:
                raise GatewayError("invalid_tool_choice", 400)
        if chat.response_format:
            fmt = chat.response_format
            if fmt.get("type") not in {"text", "json_object", "json_schema"} or set(fmt) - {"type", "json_schema"}:
                raise GatewayError("invalid_response_format", 400)
        for m in chat.messages:
            if isinstance(m.content, list) and any(set(x) != {"type", "text"} or x["type"] != "text"
                                                 or not isinstance(x["text"], str) for x in m.content):
                raise GatewayError("only_text_content_supported", 400)
        body = chat.model_dump(exclude_none=True)
        body["model"] = self.s.upstream_for(model)
        if "max_completion_tokens" not in body:
            body["max_tokens"] = output
        if chat.stream:
            body["stream_options"] = {"include_usage": True}
        # Deliberately conservative accounting units, not a tokenizer or monetary hard cap.
        units = len(json.dumps(body, ensure_ascii=False).encode()) + 1024 + output
        if units > model.context_length:
            raise GatewayError("context_reservation_exceeded", 400)
        return body, units

    async def reserve(self, owner, credential, model, units, request_id):
        async with self.db.tx() as c:
            p = await one(c, "SELECT * FROM principals WHERE id=:p FOR UPDATE", p=owner)
            gc = await one(c, "SELECT revoked_at,expires_at FROM gateway_credentials WHERE id=:id", id=credential)
            if p["status"] != "active" or gc["revoked_at"] or gc["expires_at"] <= utcnow():
                raise GatewayError("access_revoked", 403)
            pol = await one(c, "SELECT * FROM policies WHERE id=:id", id=p["policy_id"])
            if model not in {m.id for m in self.s.allowed_models(pol)}:
                raise GatewayError("model_not_allowed", 403)
            rate = await one(c, """INSERT INTO rate_windows(principal_id,minute,count)
                VALUES (:p,:m,1) ON CONFLICT(principal_id,minute) DO UPDATE SET count=rate_windows.count+1 RETURNING count""",
                p=owner, m=int(time.time()) // 60)
            if rate["count"] > pol["rate_limit"]:
                raise GatewayError("principal_rate_limited", 429, 60 - int(time.time()) % 60)
            used = await one(c, """SELECT COALESCE((SELECT charged_units FROM usage_daily
                WHERE principal_id=:p AND day=(now() AT TIME ZONE 'UTC')::date),0) AS daily,
                (SELECT count(*) FROM usage_records WHERE principal_id=:p
                AND outcome='in_flight' AND lease_expires_at>now()) AS active""", p=owner)
            if used["active"] >= pol["concurrency"]:
                raise GatewayError("principal_concurrency_limited", 429, 1)
            if p["usage_charged_units"] + units > pol["total_units"] or used["daily"] + units > pol["daily_units"]:
                raise GatewayError("principal_budget_exhausted", 429, 3600)
            await run(c, """INSERT INTO usage_records(id,principal_id,model,reserved_units,charged_units,lease_expires_at)
                VALUES (:id,:p,:m,:u,:u,:exp)""", id=request_id, p=owner, m=model, u=units,
                exp=utcnow() + timedelta(seconds=self.s.request_timeout_seconds + 5))
            await audit(c, owner, "inference", model, "started", request_id)

    async def finish(self, request_id, usage: Usage, outcome: str):
        observed = usage.prompt + usage.completion if usage.prompt is not None and usage.completion is not None else 0
        async with self.db.tx() as c:
            await run(c, """UPDATE usage_records SET outcome=:o,finished_at=now(),prompt_tokens=:p,completion_tokens=:t,
                provider_cost=:cost,cost_provenance=:prov,
                charged_units=greatest(reserved_units,CAST(:observed AS bigint)) WHERE id=:id""",
                id=request_id, o=outcome, p=usage.prompt, t=usage.completion, cost=usage.cost,
                observed=observed, prov="provider_reported" if usage.cost is not None else "unknown")
            await audit(c, "gateway", "inference", request_id, outcome, request_id)


async def sse_frames(response):
    """Bounded incremental framing; retain at most one event, never a completion transcript."""
    buf = b""
    async for chunk in response.aiter_bytes():
        buf += chunk
        # Normalize CRLF after buffering so a boundary may span network chunks.
        buf = buf.replace(b"\r\n", b"\n")
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            if len(frame) > 65536:
                raise GatewayError("provider_sse_frame_too_large", 502)
            yield frame
        if len(buf) > 65536:
            raise GatewayError("provider_sse_frame_too_large", 502)
    if buf.strip():
        raise GatewayError("provider_sse_truncated", 502)


async def proxy_sse(response, usage, alias):
    done = False
    async for frame in sse_frames(response):
        data = b"\n".join(x[5:].lstrip() for x in frame.split(b"\n") if x.startswith(b"data:"))
        if not data:
            yield b": keepalive\n\n"
            continue
        if data == b"[DONE]":
            done = True
            yield b"data: [DONE]\n\n"
            break
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise GatewayError("provider_sse_invalid", 502) from None
        usage.observe(obj)
        if obj.get("error"):
            raise GatewayError("provider_stream_error", 502)
        if "model" in obj:
            obj["model"] = alias
        yield ("data: " + json.dumps(obj, separators=(",", ":")) + "\n\n").encode()
    if not done:
        raise GatewayError("provider_stream_incomplete", 502)


async def bounded_json(response, limit=8388608):
    content = bytearray()
    async for part in response.aiter_bytes():
        content.extend(part)
        if len(content) > limit:
            raise GatewayError("provider_response_too_large", 502)
    try:
        obj = json.loads(content)
        if not isinstance(obj, dict):
            raise ValueError()
        return obj
    except ValueError:
        raise GatewayError("provider_response_invalid", 502) from None
