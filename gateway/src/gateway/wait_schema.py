"""Init-container gate: wait for the required database migration."""
import asyncio
import logging
import time

from gateway.config import Settings
from gateway.db import DB, one


async def main():
    s = Settings()
    db = DB(s.database_url.get_secret_value(), s.database_schema, pool_size=1)
    deadline = time.monotonic() + 300
    try:
        while time.monotonic() < deadline:
            try:
                async with db.tx() as conn:
                    row = await one(conn, "SELECT version_num FROM alembic_version")
                if row and row["version_num"] == "002":
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        raise SystemExit("Required database migration 002 did not complete")
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    logging.getLogger("sqlalchemy").setLevel(logging.CRITICAL)
    asyncio.run(main())
