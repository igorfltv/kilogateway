import asyncio
import ssl
import httpx
from gateway.config import Settings
from gateway.catalog import validate_upstream_catalog
from gateway.errors import GatewayError
from providers.factory import providers


async def main(settings=None):
    s = settings or Settings()
    if s.provider_mode == "corporate":
        raise GatewayError("provider_not_configured")
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(15, read=s.request_timeout_seconds),
            verify=ssl.create_default_context(cafile=s.tls_ca_bundle) if s.tls_ca_bundle else True) as c:
        p, _ = providers(s, c)
        validate_upstream_catalog(s, await p.list_models())
    scope = "model IDs verified; limits/tools declared by operator" if (
        s.provider_mode == "openai-compatible" and s.provider_catalog_validation == "ids"
    ) else "models, limits and tools match provider catalog"
    print(f"PASS: {len(s.models)} {scope}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        raise SystemExit(e.code if isinstance(e, GatewayError) else "model_validation_failed") from None
