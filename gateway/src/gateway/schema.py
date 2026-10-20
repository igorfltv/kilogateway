"""Schema initialization for isolated tests only; deployments use Alembic."""
from pathlib import Path

from gateway.db import run


async def initialize_test_schema(connection, migrations: Path):
    for statement in (migrations / "schema.sql").read_text().split(";"):
        if statement.strip():
            await run(connection, statement)
    await run(connection, (migrations / "usage_counters.sql").read_text())
