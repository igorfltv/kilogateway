from pathlib import Path

from alembic import op

revision = "003"
down_revision = "002"


def upgrade():
    for statement in (Path(__file__).parents[1] / "mcp.sql").read_text().split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("MCP credentials must be retained; roll back application only")
