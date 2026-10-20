from pathlib import Path
from alembic import op

revision = "001"
down_revision = None


def upgrade():
    for statement in (Path(__file__).parents[1] / "schema.sql").read_text().split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally manual; restore a tested backup")
