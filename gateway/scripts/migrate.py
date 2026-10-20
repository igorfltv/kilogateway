import subprocess
from pathlib import Path
import os
import sys
from gateway.config import Settings

s = Settings()
env = {**os.environ, "DATABASE_URL": s.database_url.get_secret_value(), "DATABASE_SCHEMA": s.database_schema}
config = Path(__file__).resolve().parents[1] / "alembic.ini"
raise SystemExit(subprocess.call([sys.executable, "-m", "alembic", "-c", str(config), "upgrade", "head"], env=env))
