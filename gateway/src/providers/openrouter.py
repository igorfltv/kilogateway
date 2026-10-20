from urllib.parse import quote
from datetime import datetime, timezone

import httpx

from gateway.errors import GatewayError
from providers.base import Capabilities, Provisioned
from providers.openai_compatible import OpenAICompatibleAdapter, upstream_error as upstream_error


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """Management and inference share no Authorization headers or client defaults."""
    def __init__(self, client: httpx.AsyncClient, base: str, management_key: str, *, management_base=None,
                 inference_base=None, catalog_base=None, keys_path="/keys", models_path="/models",
                 chat_path="/chat/completions"):
        self.client, self.base, self.management_key = client, base.rstrip("/"), management_key
        self.management_base = (management_base or base).rstrip("/")
        self.inference_base = (inference_base or base).rstrip("/")
        self.catalog_base = (catalog_base or base).rstrip("/")
        self.catalog_key = ""
        self.keys_path, self.models_path, self.chat_path = keys_path, models_path, chat_path

    def capabilities(self):
        return Capabilities(True, True)

    async def management(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        try:
            r = await self.client.request(method, self.management_base + path, json=body,
                                          headers={"Authorization": f"Bearer {self.management_key}"}, follow_redirects=False)
        except httpx.HTTPError:
            raise GatewayError("provider_management_unavailable") from None
        if method == "DELETE" and r.status_code == 404:
            return r
        if r.status_code >= 300:
            raise upstream_error(r.status_code)
        return r

    async def provision(self, label: str, limit: float, expires_at: str):
        # OpenRouter validates UTC RFC3339 with Z; Python's +00:00 form is rejected by the live API.
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise GatewayError("provider_expiry_timezone_required", 400)
        expires_at = expiry.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        r = await self.management("POST", self.keys_path, {"name": label, "limit": limit,
                    "limit_reset": None, "expires_at": expires_at, "include_byok_in_limit": True})
        try:
            d = r.json()
            if not isinstance(d["key"], str) or not isinstance(d["data"]["hash"], str):
                raise ValueError()
            return Provisioned(d["data"]["hash"], d["key"])
        except (ValueError, KeyError, TypeError):
            raise GatewayError("provider_create_ambiguous") from None

    async def inspect(self, reference: str):
        r = await self.management("GET", self.keys_path + "/" + quote(reference, safe=""))
        d = r.json()["data"]
        return {"disabled": bool(d.get("disabled")), "expires_at": d.get("expires_at"),
                "usage": d.get("usage"), "limit": d.get("limit")}

    async def revoke(self, reference: str):
        await self.management("DELETE", self.keys_path + "/" + quote(reference, safe=""))

    async def find(self, label: str):
        result, offset = [], 0
        # Bounded per pass; fail closed rather than overlook an orphan on a truncated list.
        for _ in range(100):
            r = await self.management("GET", f"{self.keys_path}?include_disabled=true&offset={offset}")
            data = r.json()["data"]
            result.extend(d["hash"] for d in data if d.get("name") == label)
            if not data:
                return result
            offset += len(data)
        raise GatewayError("provider_reconciliation_page_limit")
