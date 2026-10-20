import asyncio
import os
import re

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine


def upgrade(conn):
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    schema = os.environ.get("DATABASE_SCHEMA", "public")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("invalid database schema")
    engine = create_async_engine(os.environ["DATABASE_URL"], hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema, "timezone": "UTC", "lock_timeout": "30000"}})
    async with engine.connect() as conn:
        # Serialize multiple replicas/migrators before reading Alembic version.
        await conn.exec_driver_sql("SELECT pg_advisory_lock(91472610)")
        await conn.commit()
        await conn.run_sync(upgrade)
        await conn.exec_driver_sql("SELECT pg_advisory_unlock(91472610)")
        await conn.commit()
    await engine.dispose()


asyncio.run(main())
