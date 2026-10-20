"""OpenAI Chat Completions transport, independent of credential provisioning."""
import httpx

from gateway.errors import GatewayError


def upstream_error(status: int) -> GatewayError:
    code, client_status = {
        401: ("provider_key_rejected", 502), 403: ("provider_permission_denied", 502),
        402: ("provider_insufficient_balance", 502), 429: ("provider_rate_limited", 429),
    }.get(status, ("provider_unavailable", 502))
    return GatewayError(code, client_status, 30 if status == 429 else None)


class OpenAICompatibleAdapter:
    def __init__(self, client: httpx.AsyncClient, base: str, *, catalog_base: str | None = None,
                 catalog_key: str = "", models_path="/models", chat_path="/chat/completions"):
        self.client = client
        self.inference_base = base.rstrip("/")
        self.catalog_base = (catalog_base or base).rstrip("/")
        self.catalog_key = catalog_key
        self.models_path, self.chat_path = models_path, chat_path

    async def list_models(self):
        # An authenticated catalog uses its own server credential, never a management key.
        headers = {"Authorization": f"Bearer {self.catalog_key}"} if self.catalog_key else {}
        try:
            response = await self.client.get(self.catalog_base + self.models_path, headers=headers,
                                             follow_redirects=False)
            if response.status_code != 200:
                raise GatewayError("provider_catalog_unavailable")
            data = response.json()["data"]
            if not isinstance(data, list) or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in data):
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise GatewayError("provider_catalog_unavailable") from None

    async def chat(self, request: dict, secret: str):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        req = self.client.build_request("POST", self.inference_base + self.chat_path, json=request, headers=headers)
        try:
            response = await self.client.send(req, stream=True, follow_redirects=False)
        except httpx.HTTPError:
            raise GatewayError("provider_unavailable", 502) from None
        if response.status_code >= 300:
            await response.aclose()
            raise upstream_error(response.status_code)
        return response
