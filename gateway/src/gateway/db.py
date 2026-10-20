from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class DB:
    def __init__(self, url: str, schema: str = "public", *, pool_size=12, max_overflow=0,
                 pool_timeout=3, statement_timeout_ms=5000):
        self.engine = create_async_engine(url, pool_size=pool_size, max_overflow=max_overflow,
                                         pool_timeout=pool_timeout, pool_pre_ping=True, pool_use_lifo=True,
                                         echo=False, hide_parameters=True,
                                         connect_args={"server_settings": {"search_path": schema, "timezone": "UTC",
                                             "statement_timeout": str(statement_timeout_ms)}})

    @asynccontextmanager
    async def tx(self):
        async with self.engine.begin() as conn:
            yield conn


async def rows(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).mappings().all()


async def one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).mappings().first()


async def run(conn, sql: str, **params):
    return await conn.execute(text(sql), params)


async def audit(conn, actor: str, operation: str, target: str, outcome: str, correlation: str):
    # Closed fields: never accept exception strings, tokens, request bodies or URLs.
    await run(conn, """INSERT INTO audit_events(actor,operation,target,outcome,correlation)
                         VALUES (:a,:o,:t,:r,:c)""", a=actor, o=operation, t=target, r=outcome, c=correlation)
