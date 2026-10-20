from pathlib import Path

from dotenv import dotenv_values

PUBLIC_KEYS = {"PROFILE", "PROVIDER_MODE", "GATEWAY_PUBLIC_URL", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_AUDIENCE",
    "OIDC_INTERNAL_ORIGIN", "OIDC_ALLOWED_ORIGINS", "OIDC_DISCOVERY_URL", "OIDC_TOKEN_URL", "OIDC_DEVICE_URL",
    "OIDC_JWKS_URL", "KEYCLOAK_ADMIN_BASE_URL", "KEYCLOAK_STATUS_CLIENT_ID", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
    "PROVIDER_MANAGEMENT_BASE_URL", "PROVIDER_INFERENCE_BASE_URL", "PROVIDER_CATALOG_BASE_URL", "PROVIDER_KEYS_PATH",
    "PROVIDER_MODELS_PATH", "PROVIDER_CHAT_PATH", "CORPORATE_INFERENCE_BASE_URL", "CORPORATE_MODEL_ID",
    "CORPORATE_AUTH_MODE", "FAKE_PROVIDER_URL", "MODEL_CATALOG_FILE", "MODEL_CONTEXT", "MODEL_MAX_OUTPUT",
    "MODEL_ALIAS", "MODEL_TOOLS", "TLS_CA_BUNDLE"}
PUBLIC_KEYS |= {"PROVIDER_CREDENTIAL_MODE", "PROVIDER_CATALOG_VALIDATION"}
PUBLIC_KEYS |= {"JIRA_BASE_URL", "CONFLUENCE_BASE_URL"}
PUBLIC_KEYS |= {"PROVISION_INFERENCE_ON_LOGIN"}


def public_profile(filename):
    if not Path(filename).is_file():
        raise ValueError("endpoint profile file does not exist")
    values = dotenv_values(filename)
    if set(values) - PUBLIC_KEYS:
        raise ValueError("endpoint profile contains unsupported or secret fields")
    return {k:v for k,v in values.items() if v is not None}
