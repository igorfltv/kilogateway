"""Open-loop HTTP capacity test: real gateway workers/PostgreSQL, synthetic local upstream.

No user credentials or external model calls. Seeds old history before migration 002
to test backfill and request cost independent of growing usage history.
"""
import argparse
import asyncio
import base64
import json
import hashlib
import os
import platform
import resource
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text

from gateway.config import Settings
from gateway.db import DB, one, run
from gateway.secrets import digest

ROOT = Path(__file__).resolve().parents[1]


def source_hash():
    paths = sorted([*(ROOT/"gateway/src").rglob("*.py"), *(ROOT/"gateway/migrations").rglob("*.sql"),
        *(ROOT/"gateway/migrations").rglob("*.py"), ROOT/"gateway/uv.lock", Path(__file__), ROOT/"tools/load_fixture.py"])
    return hashlib.sha256("".join(str(p.relative_to(ROOT))+":"+hashlib.sha256(p.read_bytes()).hexdigest()+"\n"
        for p in paths).encode()).hexdigest()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def percentile(values, fraction):
    return round(sorted(values)[min(len(values)-1, int(len(values)*fraction))], 3) if values else None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--history", type=int, default=100000)
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--rates", type=int, nargs="+", default=[100, 300])
    parser.add_argument("--stream-seconds", type=float, default=3)
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--lifecycle", action="store_true")
    parser.add_argument("--output", default="artifacts/reports/gateway-load.json")
    args = parser.parse_args()
    assert 1 <= args.workers <= 8 and 1 <= args.duration <= 300 and 1 <= args.users <= 5000
    assert all(1 <= rate <= 2000 for rate in args.rates) and 0 < args.stream_seconds < args.duration
    assert not args.lifecycle or args.workers == 1
    s = Settings(oidc_internal_origin="")
    assert s.profile == "dev", "Load-test schema creation is restricted to development"
    work = Path(tempfile.mkdtemp(prefix="load-", dir=ROOT / ".local"))
    work.chmod(0o700)
    api_port, fixture_port = port(), port()
    issuer = f"http://127.0.0.1:{fixture_port}/realms/load"
    schema = "load_" + uuid4().hex
    fixture_key, hash_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    values = {"PROFILE": "dev", "DATABASE_URL": s.database_url.get_secret_value(), "DATABASE_SCHEMA": schema,
        "GATEWAY_PUBLIC_URL": f"http://127.0.0.1:{api_port}", "OIDC_ISSUER": issuer,
        "KEYCLOAK_STATUS_CLIENT_SECRET": "local-load-fixture", "PROVIDER_MODE": "openai-compatible",
        "PROVIDER_CREDENTIAL_MODE": "static", "PROVIDER_INFERENCE_BASE_URL": f"http://127.0.0.1:{fixture_port}/v1",
        "PROVIDER_API_KEY": fixture_key, "MODEL_CATALOG_FILE": "builtin:y-ai", "MODEL_CONTEXT": "262144",
        "MODEL_MAX_OUTPUT": "4096", "AUTHZ_MAX_STALENESS_SECONDS": "30", "WORKER_ENABLED": "false",
        "ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(), "CREDENTIAL_HASH_KEY": hash_key,
        "ADMIN_TOKEN": secrets.token_urlsafe(32), "DB_POOL_SIZE": "12", "MAX_INFLIGHT_REQUESTS": "512"}
    conf = work / "server.env"
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
    env.update({"PYTHONPATH": str(ROOT/"gateway/src")+os.pathsep+str(ROOT), "YAI_SECRETS_FILE": str(conf),
                "LOAD_ISSUER": issuer, "LOAD_INFERENCE_KEY": fixture_key})
    report = {"status": "RUNNING", "workers": args.workers, "users": args.users, "historical_records": args.history,
        "source_sha256": source_hash(),
        "generator_clients": args.clients, "authz_max_staleness_seconds": 30,
        "admission_limit_per_worker": 512, "db_pool_per_worker": 12, "upstream": "synthetic-loopback-only",
        "identity": "synthetic-status-fixture; preissued gateway credentials", "hardware": {"platform": platform.platform(),
        "cpu_count": os.cpu_count()}, "scenarios": []}
    processes = []
    db = DB(s.database_url.get_secret_value(), schema, statement_timeout_ms=60000)
    setup = DB(s.database_url.get_secret_value(), statement_timeout_ms=60000)
    created = False
    keys = []
    generators = [httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=256, keepalive_expiry=30)) for _ in range(args.clients)]
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, max(soft, 8192)), hard))

    async def launch(module, bind_port, workers=1):
        logfile = work / (module.rsplit(".", 1)[-1] + ".log")
        logfile.touch(mode=0o600)
        with logfile.open("wb") as output:
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "uvicorn", module,
                *( ["--factory"] if module.endswith(":create_app") else []), "--host", "127.0.0.1", "--port", str(bind_port),
                "--workers", str(workers), "--no-access-log", "--timeout-graceful-shutdown", "15",
                cwd=ROOT, env=env, stdout=output, stderr=output, start_new_session=True)
        processes.append(process)

    async def ready(client, url):
        for _ in range(150):
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.1)
        raise RuntimeError("load_service_startup_failed")

    try:
        async with setup.tx() as conn:
            await run(conn, f'CREATE SCHEMA "{schema}"')
        created = True
        async with db.tx() as conn:
            for sql in (ROOT/"gateway/migrations/schema.sql").read_text().split(";"):
                if sql.strip():
                    await run(conn, sql)
            await run(conn, """INSERT INTO policies VALUES ('default',:aliases,ARRAY['openai-compatible'],
                100000,100,1000000000000,1000000000000,1)""", aliases=[m.id for m in s.models])
            principals, credentials = [], []
            for index in range(args.users):
                owner, ident = f"load_user_{index}", "gc_" + uuid4().hex
                key = "cg_" + ident + "." + secrets.token_urlsafe(32)
                keys.append(key)
                principals.append({"id": owner, "subject": f"load-{index}", "issuer": issuer})
                credentials.append({"id": ident, "owner": owner, "hash": digest(key, hash_key)})
            await conn.execute(text("""INSERT INTO principals(id,tenant,issuer,subject,policy_id,authz_allowed,authz_checked_at)
                VALUES (:id,'company',:issuer,:subject,'default',true,now())"""), principals)
            await conn.execute(text("""INSERT INTO gateway_credentials(id,principal_id,secret_hash,device_name,expires_at)
                VALUES (:id,:owner,:hash,'load-test',now()+interval '1 hour')"""), credentials)
            await run(conn, """INSERT INTO usage_records(id,principal_id,model,reserved_units,charged_units,
                created_at,lease_expires_at,outcome) SELECT 'history_'||n,'load_user_'||(n % :users),
                'history',1,1,now()-interval '2 days',now()-interval '1 day','success' FROM generate_series(1,:count) n""",
                users=args.users, count=args.history)
            await run(conn, (ROOT/"gateway/migrations/usage_counters.sql").read_text())
            for sql in (ROOT/"gateway/migrations/mcp.sql").read_text().split(";"):
                if sql.strip():
                    await run(conn, sql)
        await launch("tools.load_fixture:app", fixture_port)
        await launch("gateway.app:create_app", api_port, args.workers)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30,
                limits=httpx.Limits(max_connections=2048, max_keepalive_connections=2048, keepalive_expiry=30)) as client:
            await ready(client, f"http://127.0.0.1:{fixture_port}/health")
            await ready(client, values["GATEWAY_PUBLIC_URL"] + "/health/ready")
            for streaming in (False, True):
                for rate in args.rates:
                    started = time.monotonic()
                    results, lags, pending = [], [], set()
                    dropped = 0
                    exception_types = {}
                    cpu_started = time.process_time()
                    delay = args.stream_seconds if streaming else .04
                    async def send(index):
                        begin = time.monotonic()
                        status, first, valid = 0, None, False
                        try:
                            async with generators[index % len(generators)].stream("POST", values["GATEWAY_PUBLIC_URL"]+"/v1/chat/completions",
                                headers={"Authorization": "Bearer "+keys[index % len(keys)]}, json={"model": s.models[index % len(s.models)].id,
                                    "messages": [{"role": "user", "content": "Synthetic gateway capacity probe."}],
                                    "max_tokens": 16, "stream": streaming, "seed": int(delay*1000)}) as response:
                                status = response.status_code
                                if status == 200 and streaming:
                                    done, content, error = False, False, False
                                    async for line in response.aiter_lines():
                                        if not line.startswith("data:"):
                                            continue
                                        first = first or time.monotonic()-begin
                                        frame = line[5:].strip()
                                        if frame == "[DONE]":
                                            done = True
                                            continue
                                        event = json.loads(frame)
                                        error = error or bool(event.get("error"))
                                        content = content or any(c.get("delta", {}).get("content") for c in event.get("choices", []))
                                    valid = done and bool(content) and not error
                                else:
                                    data = await response.aread()
                                    first = time.monotonic()-begin
                                    valid = status == 200 and json.loads(data)["choices"][0]["message"]["content"] == "OK"
                        except Exception as error:
                            name = type(error).__name__
                            exception_types[name] = exception_types.get(name, 0) + 1
                            status = 0
                        results.append((status, time.monotonic()-begin, first, valid, time.monotonic()-started))
                    for index in range(rate*args.duration):
                        target = started + index/rate
                        await asyncio.sleep(max(0, target-time.monotonic()))
                        lags.append(max(0, time.monotonic()-target)*1000)
                        if len(pending) >= 1800:
                            dropped += 1
                            continue
                        task = asyncio.create_task(send(index))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    await asyncio.gather(*pending)
                    good = [r for r in results if r[0] == 200 and r[3]]
                    steady_seconds = args.duration-delay-1
                    completed_steady = sum(delay+1 <= r[4] < args.duration for r in good)
                    row = {"mode": "sse" if streaming else "json", "target_rps": rate, "offered": rate*args.duration,
                        "duration_seconds": args.duration, "upstream_delay_seconds": delay, "successes": len(good),
                        "client_dropped": dropped, "errors": len(results)-len(good),
                        "steady_completed_rps": round(completed_steady/steady_seconds, 2) if steady_seconds>0 else None,
                        "latency_p50_ms": percentile([r[1]*1000 for r in good], .5), "latency_p95_ms": percentile([r[1]*1000 for r in good], .95),
                        "latency_p99_ms": percentile([r[1]*1000 for r in good], .99),
                        "ttfb_p95_ms": percentile([r[2]*1000 for r in good if r[2] is not None], .95),
                        "scheduler_lag_p99_ms": percentile(lags,.99),
                        "generator_cpu_seconds": round(time.process_time()-cpu_started,3),
                        "exception_types": exception_types,
                        "status_counts": {str(status):sum(r[0]==status for r in results) for status in sorted({r[0] for r in results})}}
                    row["status"] = "PASS" if not dropped and len(good)==row["offered"] and (
                        row["steady_completed_rps"] is not None and row["steady_completed_rps"]>=rate*.95 and
                        row["latency_p95_ms"] < delay*1000+1000 and row["scheduler_lag_p99_ms"] < 100) else "FAIL"
                    report["scenarios"].append(row)
                    print(json.dumps(row), flush=True)
            if args.lifecycle:
                report["lifecycle_step"] = "disconnect"
                endpoint = values["GATEWAY_PUBLIC_URL"]
                body = {"model": s.models[0].id, "messages": [{"role": "user", "content": "Synthetic lifecycle probe."}],
                        "stream": True, "max_tokens": 16, "seed": int(args.stream_seconds*1000)}
                headers = {"Authorization": "Bearer "+keys[0]}
                async with client.stream("POST", endpoint+"/v1/chat/completions", headers=headers, json=body) as response:
                    assert response.status_code == 200
                    rid = response.headers["x-request-id"]
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            break
                for _ in range(100):
                    async with db.tx() as conn:
                        row = await one(conn, "SELECT outcome FROM usage_records WHERE id=:id", id=rid)
                    if row and row["outcome"] != "in_flight":
                        break
                    await asyncio.sleep(.05)
                report["disconnect_outcome"] = row["outcome"] if row else None
                assert row and row["outcome"] in {"disconnected", "client_disconnected"}
                report["lifecycle_step"] = "open_stream"
                first = asyncio.Event()
                async def active_stream():
                    done = False
                    async with client.stream("POST", endpoint+"/v1/chat/completions", headers=headers, json=body) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                first.set()
                                done = done or line[5:].strip() == "[DONE]"
                    return done
                task = asyncio.create_task(active_stream())
                await asyncio.wait_for(first.wait(), 10)
                drained = await client.post(endpoint+"/admin/drain", headers={"Authorization": "Bearer "+values["ADMIN_TOKEN"]})
                report["lifecycle_step"] = "drain"
                report["drain_status"] = drained.status_code
                report["drain_inflight"] = drained.json().get("inflight")
                assert drained.status_code == 200 and drained.json()["inflight"] == 1
                assert (await client.get(endpoint+"/health/ready")).status_code == 503
                assert (await client.get(endpoint+"/health/live")).status_code == 200
                assert (await client.post(endpoint+"/v1/chat/completions", headers=headers, json=body)).status_code == 503
                os.killpg(processes[-1].pid, signal.SIGTERM)
                report["lifecycle_step"] = "sigterm"
                report["sigterm_stream_completed"] = await asyncio.wait_for(task, 15)
                report["api_exit_code"] = await asyncio.wait_for(processes[-1].wait(), 20)
                # Uvicorn 0.34 re-raises the captured SIGTERM AFTER graceful shutdown.
                assert report["sigterm_stream_completed"]
                assert report["api_exit_code"] in {0, -signal.SIGTERM, 128+signal.SIGTERM}
                report["lifecycle"] = {"disconnect_accounted_without_refund": "PASS", "drain_readiness": "PASS",
                    "admission_rejected_during_drain": "PASS", "active_sse_completed_on_sigterm": "PASS"}
                report.pop("lifecycle_step", None)
            report["fixture"] = (await client.get(f"http://127.0.0.1:{fixture_port}/health")).json()
        async with db.tx() as conn:
            counters = await one(conn, """SELECT (SELECT sum(usage_charged_units) FROM principals) AS counters,
                (SELECT sum(charged_units) FROM usage_records) AS records,
                (SELECT count(*) FROM usage_records WHERE outcome='in_flight') AS inflight""")
        assert counters["counters"] == counters["records"] and counters["inflight"] == 0
        report["accounting_consistent"] = True
        report["status"] = "PASS" if all(r["status"]=="PASS" for r in report["scenarios"]) else "FAIL"
    except Exception as error:
        report["status"], report["error_type"] = "FAIL", type(error).__name__
        raise
    finally:
        for process in reversed(processes):
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        await asyncio.gather(*(client.aclose() for client in generators))
        await db.engine.dispose()
        if created:
            async with setup.tx() as conn:
                await run(conn, f'DROP SCHEMA "{schema}" CASCADE')
            report["isolated_schema_removed"] = True
        await setup.engine.dispose()
        report["source_unchanged"] = source_hash() == report["source_sha256"]
        if not report["source_unchanged"]:
            report["status"] = "INVALID_SOURCE_CHANGED"
        report_path = ROOT/args.output
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2)+"\n")
        conf.unlink(missing_ok=True)
        if report["status"] == "PASS":
            shutil.rmtree(work)
        print("Report: "+str(report_path.relative_to(ROOT))+"; status="+report["status"], flush=True)
    assert report["status"] == "PASS"


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Load test failed; inspect sanitized report and private local logs") from None
