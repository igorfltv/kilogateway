"""Explicit batched retention after external archival; lifetime counters are never reduced."""
import argparse
import asyncio
import json
from datetime import timedelta

from gateway.broker import utcnow
from gateway.config import Settings
from gateway.db import DB, one, rows


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--batches", type=int, default=1)
    parser.add_argument("--apply", action="store_true", help="Delete archived old rows; default is dry-run")
    args = parser.parse_args()
    assert args.days >= 1 and 1 <= args.batch <= 10000 and 1 <= args.batches <= 100
    s = Settings()
    db = DB(s.database_url.get_secret_value(), s.database_schema)
    cutoff = utcnow() - timedelta(days=args.days)
    report = {"mode": "apply" if args.apply else "dry-run", "days": args.days, "deleted": {}}
    try:
        for table, eligible in (("usage_records", "AND outcome<>'in_flight'"), ("audit_events", "")):
            removed = 0
            for _ in range(args.batches):
                async with db.tx() as conn:
                    if args.apply:
                        data = await rows(conn, f"""WITH old AS (SELECT id FROM {table}
                            WHERE created_at<:cutoff {eligible} ORDER BY created_at LIMIT :batch FOR UPDATE SKIP LOCKED)
                            DELETE FROM {table} t USING old WHERE t.id=old.id RETURNING t.id""", cutoff=cutoff, batch=args.batch)
                        removed += len(data)
                    else:
                        result = await one(conn, f"SELECT count(*) AS n FROM {table} WHERE created_at<:cutoff {eligible}", cutoff=cutoff)
                        report.setdefault("eligible", {})[table] = result["n"]
                        break
                if len(data) < args.batch:
                    break
            report["deleted"][table] = removed
        print(json.dumps(report))
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Retention failed; secrets suppressed") from None
