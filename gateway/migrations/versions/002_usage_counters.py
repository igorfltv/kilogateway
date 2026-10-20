from pathlib import Path

from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute((Path(__file__).parents[1] / "usage_counters.sql").read_text())


def downgrade():
    raise RuntimeError("Usage counters preserve lifetime accounting; rollback application only, retain schema")
