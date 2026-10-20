import os
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.catalog import ModelSpec, read_catalog
from gateway.profile import public_profile


def trusted_url(value: str, dev: bool = False) -> str:
    u = urlparse(value)
    if u.username or u.password or u.query or u.fragment or not u.hostname:
        raise ValueError("URL must not contain credentials, query or fragment")
    if u.scheme != "https" and not (dev and u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("HTTPS required; dev HTTP only on loopback")
    return value.rstrip("/")


def endpoint_path(value: str) -> str:
    import re
    if not re.fullmatch(r"/(?:[A-Za-z0-9._-]+/?)+", value) or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("invalid endpoint path")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    profile: str = "production"
    gateway_public_url: str = "https://code.ai.company.com"
    database_url: SecretStr
    database_schema: str = "public"
    db_pool_size: int = 12
    db_max_overflow: int = 0
    db_pool_timeout_seconds: float = 3
    db_statement_timeout_ms: int = 5000
    http_max_connections: int = 1024
    http_max_keepalive_connections: int = 256
    http_pool_timeout_seconds: float = 2
    identity_max_connections: int = 32
    identity_timeout_seconds: float = 5
    max_inflight_requests: int = 512
    worker_enabled: bool = True
    metrics_enabled: bool = True
    oidc_issuer: str = "https://code.ai.company.com/realms/company"
    oidc_client_id: str = "kilo-cli"
    oidc_audience: str = "code-ai-api"
    oidc_internal_origin: str | None = None
    oidc_allowed_origins: list[str] = Field(default_factory=list)
    oidc_discovery_url: str = ""
    oidc_token_url: str = ""
    oidc_device_url: str = ""
    oidc_jwks_url: str = ""
    keycloak_admin_base_url: str = ""
    keycloak_status_client_id: str = "gateway-status"
    keycloak_status_client_secret: SecretStr
    gateway_credential_ttl_seconds: int = 28800
    authz_max_staleness_seconds: int = 30
    provider_mode: str = "openai-compatible"
    provider_credential_mode: str = "static"
    provider_api_key: SecretStr = SecretStr("")
    provider_catalog_api_key: SecretStr = SecretStr("")
    provider_catalog_validation: str = "ids"
    corporate_inference_base_url: str = ""
    corporate_model_id: str = ""
    corporate_auth_mode: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    provider_management_base_url: str = ""
    provider_inference_base_url: str = ""
    provider_catalog_base_url: str = ""
    provider_keys_path: str = "/keys"
    provider_models_path: str = "/models"
    provider_chat_path: str = "/chat/completions"
    openrouter_management_key: SecretStr = SecretStr("")
    openrouter_model: str = ""
    openrouter_test_key_limit_usd: float = 0.25
    provider_key_ttl_seconds: int = 3600
    fake_provider_url: str = "http://fake-provider:8090/v1"
    fake_management_key: SecretStr = SecretStr("")
    encryption_key: SecretStr
    encryption_key_version: str = "v1"
    credential_hash_key: SecretStr
    admin_token: SecretStr
    tenant: str = "company"
    model_catalog_file: str = "builtin:y-ai"
    model_catalog: list[ModelSpec] = Field(default_factory=list)
    model_alias: str = "company-coder"
    model_context: int = 32768
    model_max_output: int = 4096
    model_tools: bool = True
    request_rate: int = 30
    request_concurrency: int = 3
    daily_token_units: int = 1000000
    total_token_units: int = 10000000
    principal_provider_budget_usd: float = 1.0
    body_limit: int = 1048576
    body_read_timeout_seconds: float = 10
    request_timeout_seconds: int = 120
    worker_interval_seconds: float = 2
    tls_ca_bundle: str = ""

    def __init__(self, **values):
        if "_env_file" not in values:
            secret_file = os.environ.get("YAI_SECRETS_FILE", ".env")
            selectors = dotenv_values(secret_file)
            sources = [secret_file]
            extra = os.environ.get("YAI_EXTRA_SECRETS_FILE", selectors.get("YAI_EXTRA_SECRETS_FILE"))
            endpoint = os.environ.get("YAI_ENDPOINTS_FILE", selectors.get("YAI_ENDPOINTS_FILE"))
            if extra:
                sources.append(extra)
            if endpoint:
                public_profile(endpoint)
                sources.append(endpoint)
            values["_env_file"] = tuple(sources)
        super().__init__(**values)

    @model_validator(mode="after")
    def valid(self):
        import re
        for field in ("database_url", "keycloak_status_client_secret", "provider_api_key", "provider_catalog_api_key",
                      "openrouter_management_key", "fake_management_key", "encryption_key", "credential_hash_key", "admin_token"):
            value = getattr(self, field).get_secret_value()
            if re.search(r"(?:^|[:/@])(?:REPLACE(?:_[A-Z0-9]+)*|CHANGE_ME|CHANGEME)(?=$|[:/@?])", value):
                raise ValueError(f"{field} contains an unconfigured placeholder")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.database_schema):
            raise ValueError("invalid database schema")
        if self.profile not in {"production", "test", "dev"}:
            raise ValueError("profile must be production, test or dev")
        if self.model_catalog_file:
            self.model_catalog = read_catalog(self.model_catalog_file)
        if len({m.id for m in self.model_catalog}) != len(self.model_catalog):
            raise ValueError("duplicate model IDs")
        dev = self.profile == "dev"
        self.gateway_public_url = trusted_url(self.gateway_public_url, dev)
        self.oidc_issuer = trusted_url(self.oidc_issuer, dev)
        issuer_origin = "{0.scheme}://{0.netloc}".format(urlparse(self.oidc_issuer))
        for origin in self.oidc_allowed_origins:
            u = urlparse(trusted_url(origin, dev))
            if u.path not in {"", "/"}:
                raise ValueError("OIDC allowlist requires origins")
        self.oidc_allowed_origins = [o.rstrip("/") for o in self.oidc_allowed_origins]
        allowed = {issuer_origin, *(o.rstrip("/") for o in self.oidc_allowed_origins)}
        for url in (self.oidc_discovery_url, self.oidc_token_url, self.oidc_device_url, self.oidc_jwks_url,
                    self.keycloak_admin_base_url):
            if url:
                u = urlparse(trusted_url(url, dev))
                if f"{u.scheme}://{u.netloc}" not in allowed:
                    raise ValueError("explicit OIDC endpoint requires a trusted origin")
        if not 1 <= self.authz_max_staleness_seconds <= 60:
            raise ValueError("authz freshness must be 1..60 seconds")
        if not 60 <= self.gateway_credential_ttl_seconds <= 86400:
            raise ValueError("credential TTL must be 60..86400")
        if self.profile == "production" and (self.provider_mode not in {"corporate", "openai-compatible"} or
                (self.provider_mode == "openai-compatible" and self.provider_credential_mode == "openrouter-managed")):
            raise ValueError("production requires an explicit corporate or OpenAI-compatible endpoint; no external fallback")
        if self.provider_mode not in {"corporate", "fake", "openrouter-managed", "openai-compatible"}:
            raise ValueError("unsupported provider mode")
        if self.provider_credential_mode not in {"static", "none", "openrouter-managed"}:
            raise ValueError("unsupported provider credential mode")
        if self.provider_catalog_validation not in {"ids", "metadata"}:
            raise ValueError("unsupported catalog validation mode")
        if self.provider_mode == "openai-compatible":
            if not self.provider_inference_base_url or not self.model_catalog:
                raise ValueError("OpenAI-compatible inference base URL and model catalog required")
            if self.provider_credential_mode == "static" and not self.provider_api_key.get_secret_value():
                raise ValueError("OpenAI-compatible server API key required; select none explicitly for an unauthenticated upstream")
            if self.provider_credential_mode == "none" and self.provider_api_key.get_secret_value():
                raise ValueError("unauthenticated upstream must not configure an inference key")
            if self.provider_credential_mode == "openrouter-managed" and self.provider_api_key.get_secret_value():
                raise ValueError("managed upstream must not configure a shared inference key")
        if self.corporate_inference_base_url:
            trusted_url(self.corporate_inference_base_url)
        if self.provider_mode == "fake" and not dev:
            raise ValueError("fake is dev only")
        if self.credential_provider == "openrouter-managed":
            if not (self.openrouter_model or self.model_catalog):
                raise ValueError("explicit provider model required")
            if not self.openrouter_management_key.get_secret_value():
                raise ValueError("OpenRouter management credential required")
        trusted_url(self.openrouter_base_url, dev)
        for field in ("provider_management_base_url", "provider_inference_base_url", "provider_catalog_base_url"):
            if value := getattr(self, field):
                setattr(self, field, trusted_url(value, dev))
        for path in (self.provider_keys_path, self.provider_models_path, self.provider_chat_path):
            endpoint_path(path)
        if self.oidc_internal_origin:
            u = urlparse(self.oidc_internal_origin)
            if u.path not in {"", "/"} or u.query or u.fragment or u.username or u.password:
                raise ValueError("internal OIDC route must be an origin")
            if not dev:
                trusted_url(self.oidc_internal_origin)
        if not 0 < self.openrouter_test_key_limit_usd <= self.principal_provider_budget_usd:
            raise ValueError("invalid provider allocation")
        for secret in (self.admin_token, self.credential_hash_key):
            if len(secret.get_secret_value()) < 32:
                raise ValueError("admin and hashing secrets require >=32 characters")
        if min(self.model_context, self.model_max_output, self.request_rate, self.request_concurrency,
               self.daily_token_units, self.total_token_units, self.request_timeout_seconds) <= 0:
            raise ValueError("limits must be positive")
        if min(self.db_pool_size, self.db_pool_timeout_seconds, self.db_statement_timeout_ms,
               self.http_max_connections, self.identity_max_connections, self.identity_timeout_seconds,
               self.http_pool_timeout_seconds, self.max_inflight_requests, self.body_read_timeout_seconds) <= 0 or self.db_max_overflow < 0:
            raise ValueError("runtime capacity settings must be positive")
        if not 0 <= self.http_max_keepalive_connections <= self.http_max_connections:
            raise ValueError("invalid HTTP keepalive capacity")
        return self

    @property
    def credential_provider(self):
        # Preserve managed credential identity, encryption AAD, allocation and jobs when
        # moving an existing OpenRouter installation to the generic inference transport.
        if self.provider_mode == "openai-compatible" and self.provider_credential_mode == "openrouter-managed":
            return "openrouter-managed"
        return self.provider_mode

    @property
    def shared_provider_credential(self):
        return self.provider_mode == "openai-compatible" and self.provider_credential_mode in {"static", "none"}

    @property
    def upstream_model(self):
        if self.provider_mode == "fake":
            return "fake/tool-coder"
        return self.corporate_model_id if self.provider_mode == "corporate" else self.openrouter_model

    @property
    def models(self) -> list[ModelSpec]:
        configured = self.model_catalog or [ModelSpec(id=self.model_alias, name=self.model_alias,
            upstream_id=self.upstream_model or self.model_alias, context_length=self.model_context,
            max_output_tokens=self.model_max_output, tools=self.model_tools)]
        return [m.model_copy(update={"context_length": min(m.context_length, self.model_context),
            "max_output_tokens": min(m.max_output_tokens, self.model_max_output, self.model_context),
            "tools": m.tools and self.model_tools}) for m in configured]

    def allowed_models(self, policy) -> list[ModelSpec]:
        if self.provider_mode not in policy["providers"]:
            return []
        return [m for m in self.models if m.id in policy["aliases"]]

    def upstream_for(self, model: ModelSpec) -> str:
        return "fake/tool-coder" if self.provider_mode == "fake" else model.upstream_id

    @property
    def provider_options(self):
        return {"management_base": self.provider_management_base_url or None,
                "inference_base": self.provider_inference_base_url or None,
                "catalog_base": self.provider_catalog_base_url or None,
                "keys_path": self.provider_keys_path, "models_path": self.provider_models_path,
                "chat_path": self.provider_chat_path}
