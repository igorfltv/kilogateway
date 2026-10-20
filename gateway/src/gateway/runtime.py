"""Bounded admission and metrics over the complete ASGI response, including SSE."""
import asyncio
import logging
import time
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette.responses import JSONResponse

from gateway.broker import uid


@dataclass
class Runtime:
    capacity: int
    active: int = 0
    draining: bool = False

    def __post_init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("yai_http_requests", "Completed HTTP responses", ["route", "method", "status"], registry=self.registry)
        self.duration = Histogram("yai_http_duration_seconds", "Full response duration including SSE", ["route", "method"],
            buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120, 180), registry=self.registry)
        self.inflight = Gauge("yai_http_inflight", "Active admitted requests including SSE", registry=self.registry)
        self.rejected = Counter("yai_http_admission_rejected", "Requests rejected before body/auth/upstream", registry=self.registry)
        Gauge("yai_http_capacity", "Per-process admission capacity", registry=self.registry).set(self.capacity)


class RuntimeMiddleware:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        r = self.runtime
        scope.setdefault("state", {})["request_id"] = request_id = uid("req_")
        started, status, headers_sent = time.monotonic(), 500, False
        exempt = scope["path"] in {"/health/live", "/health/ready", "/metrics", "/admin/drain"}
        admitted = not exempt and not r.draining and r.active < r.capacity

        async def response_send(message):
            nonlocal status, headers_sent
            if message["type"] == "http.response.start":
                status, headers_sent = message["status"], True
                message["headers"] = [(k, v) for k, v in message.get("headers", [])
                    if k.lower() not in {b"cache-control", b"x-request-id", b"x-content-type-options"}] + [
                    (b"cache-control", b"no-store"), (b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff")]
            await send(message)

        if admitted:
            # No await between capacity check and increment: atomic on this event loop.
            r.active += 1
            r.inflight.inc()
        try:
            if not exempt and not admitted:
                r.rejected.inc()
                return await JSONResponse({"error": {"code": "gateway_draining" if r.draining else "gateway_overloaded"}},
                    status_code=503, headers={"Retry-After": "1"})(scope, receive, response_send)
            await self.app(scope, receive, response_send)
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception:
            logging.getLogger("gateway").error("request_failed")
            if headers_sent:
                raise  # Never attempt a second status or retry an accepted inference request.
            await JSONResponse({"error": {"code": "internal_error"}}, status_code=500)(scope, receive, response_send)
        finally:
            if admitted:
                r.active -= 1
                r.inflight.dec()
            if scope["path"] != "/metrics":
                route = getattr(scope.get("route"), "path", "unmatched")
                # Unknown paths and arbitrary HTTP method strings cannot grow metric cardinality.
                method = scope["method"] if scope["method"] in {"GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"} else "OTHER"
                r.requests.labels(route, method, str(status)).inc()
                r.duration.labels(route, method).observe(time.monotonic() - started)
