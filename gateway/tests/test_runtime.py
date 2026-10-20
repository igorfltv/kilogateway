import asyncio

import pytest
from pydantic import ValidationError
from prometheus_client import generate_latest

from gateway.config import Settings
from gateway.runtime import Runtime, RuntimeMiddleware
from gateway.app import BodyLimit


@pytest.mark.parametrize("values", [{"db_pool_size": 0}, {"http_max_connections": 0},
    {"http_max_keepalive_connections": 2048}, {"max_inflight_requests": 0}])
def test_capacity_configuration_rejects_invalid_limits(settings, values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**settings.model_dump(), **values})


async def test_admission_includes_stream_body_and_rejects_before_inner_app():
    runtime = Runtime(1)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        entered.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

    middleware = RuntimeMiddleware(app, runtime)

    async def request(path="/v1/chat/completions"):
        messages = []
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "path": path, "method": "POST"}, None, send)
        return messages

    first = asyncio.create_task(request())
    await entered.wait()
    assert runtime.active == 1
    rejected = await request()
    assert calls == 1 and rejected[0]["status"] == 503 and b"gateway_overloaded" in rejected[1]["body"]
    assert (b"retry-after", b"1") in rejected[0]["headers"]
    release.set()
    await first
    assert runtime.active == 0
    runtime.draining = True
    assert b"gateway_draining" in (await request())[1]["body"]
    assert calls == 1
    metrics = generate_latest(runtime.registry)
    assert b"yai_http_admission_rejected_total 2.0" in metrics and b"yai_http_inflight 0.0" in metrics


async def test_cancelled_stream_releases_capacity_and_does_not_leak_path_labels():
    runtime = Runtime(1)
    started = asyncio.Event()
    async def app(scope, receive, send):
        started.set()
        await asyncio.Event().wait()
    async def send(message):
        pass
    task = asyncio.create_task(RuntimeMiddleware(app, runtime)(
        {"type": "http", "path": "/secret-user-id/unknown", "method": "CUSTOM-ID"}, None, send))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.active == 0
    metrics = generate_latest(runtime.registry)
    assert b"secret-user-id" not in metrics and b"CUSTOM-ID" not in metrics
    assert b'method="OTHER",route="unmatched",status="499"' in metrics


async def test_slow_request_body_times_out_before_authentication():
    called, responses = False, []
    async def app(scope, receive, send):
        nonlocal called
        called = True
    async def receive():
        await asyncio.sleep(1)
    async def send(message):
        responses.append(message)
    await BodyLimit(app, 1024, timeout=.01)({"type": "http"}, receive, send)
    assert not called and responses[0]["status"] == 408
