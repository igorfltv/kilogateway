from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class Capabilities:
    managed_keys: bool
    reconciliation: bool
    idempotent_create: bool = False


@dataclass(repr=False)
class Provisioned:
    reference: str
    secret: str


class ProviderCredentialManager(Protocol):
    def capabilities(self) -> Capabilities: ...
    async def provision(self, label: str, limit: float, expires_at: str) -> Provisioned: ...
    async def inspect(self, reference: str) -> dict: ...
    async def revoke(self, reference: str) -> None: ...
    async def find(self, label: str) -> list[str]: ...


class InferenceProvider(Protocol):
    async def list_models(self) -> list[dict]: ...
    async def chat(self, request: dict, secret: str) -> httpx.Response: ...
