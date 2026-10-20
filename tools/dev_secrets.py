"""Explicit development bootstrap: local random secrets, never public default passwords."""
import base64
import os
import secrets
from pathlib import Path

p = Path(".env")
if p.exists():
    raise SystemExit(".env already exists; preserving existing secrets")
values = {
    "PROFILE": "dev", "GATEWAY_PUBLIC_URL": "http://localhost:18000",
    "OIDC_ISSUER": "http://localhost:28080/realms/company", "OIDC_CLIENT_ID": "kilo-cli",
    "OIDC_AUDIENCE": "code-ai-api", "PROVIDER_MODE": "fake",
    "POSTGRES_PASSWORD": secrets.token_hex(24), "KC_ADMIN_USER": "dev-admin",
    "KC_ADMIN_PASSWORD": secrets.token_urlsafe(32), "DEV_USER_PASSWORD": secrets.token_urlsafe(24),
    "DEV_DENIED_PASSWORD": secrets.token_urlsafe(24), "KEYCLOAK_STATUS_CLIENT_ID": "gateway-status",
    "KEYCLOAK_STATUS_CLIENT_SECRET": secrets.token_urlsafe(32),
    "ENCRYPTION_KEY": base64.b64encode(os.urandom(32)).decode(),
    "ENCRYPTION_KEY_VERSION": "v1", "CREDENTIAL_HASH_KEY": secrets.token_urlsafe(48),
    "ADMIN_TOKEN": secrets.token_urlsafe(48), "FAKE_MANAGEMENT_KEY": secrets.token_urlsafe(48),
    "GATEWAY_CREDENTIAL_TTL_SECONDS": "28800", "AUTHZ_MAX_STALENESS_SECONDS": "2",
    "ENABLE_LIVE_OPENROUTER_TESTS": "0",
    "MODEL_CONTEXT": "262144",
    "MODEL_CATALOG_FILE": "builtin:y-ai",
}
values["DATABASE_URL"] = f"postgresql+asyncpg://gateway:{values['POSTGRES_PASSWORD']}@localhost:15432/gateway"
values["FAKE_PROVIDER_URL"] = "http://localhost:18090/v1"
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("# Generated explicitly for local development. Never commit.\n")
    f.writelines(f"{k}={v}\n" for k, v in values.items())
print("Created .env (0600); credentials were not printed")
