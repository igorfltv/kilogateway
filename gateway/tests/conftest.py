import base64
import os

import pytest

from gateway.config import Settings


@pytest.fixture
def settings():
    return Settings(_env_file=None, profile="dev", model_catalog_file="", model_catalog=[],
        gateway_public_url="http://localhost:18000",
        oidc_issuer="http://localhost:28080/realms/company", database_url="postgresql+asyncpg://unused@localhost/unused",
        keycloak_status_client_secret="test-status", encryption_key=base64.b64encode(os.urandom(32)).decode(),
        credential_hash_key="hash"*16, admin_token="admin"*16, provider_mode="fake")
