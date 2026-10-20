from gateway.errors import GatewayError
from providers.base import Capabilities


class CorporateProviderAdapter:
    """No HTTP contract has been supplied. Never invent an endpoint or fall back externally."""
    def capabilities(self):
        return Capabilities(False, False)

    async def provision(self, *args, **kwargs):
        raise GatewayError("provider_not_configured")

    inspect = revoke = find = list_models = chat = provision
