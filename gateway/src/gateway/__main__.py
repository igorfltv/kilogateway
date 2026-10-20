"""Container entrypoint: apply the selected public profile, then execute the requested service."""
import os
import sys

from gateway.profile import public_profile

profile = os.environ.get("YAI_ENDPOINTS_FILE")
if profile:
    # Explicit profile replaces legacy public values mixed into an old Docker env_file.
    os.environ.update(public_profile(profile))
command = sys.argv[1:] or ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0",
                          "--port", "8000", "--no-access-log"]
os.execvp(command[0], command)
