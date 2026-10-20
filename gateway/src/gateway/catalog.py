import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gateway.errors import GatewayError


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    name: str = Field(min_length=1, max_length=200)
    upstream_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    context_length: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    tools: bool = True

    @model_validator(mode="after")
    def limits(self):
        if self.max_output_tokens > self.context_length:
            raise ValueError("model output exceeds context")
        return self


def read_catalog(filename: str) -> list[ModelSpec]:
    try:
        path = Path(__file__).with_name("models.y-ai.json") if filename == "builtin:y-ai" else Path(filename)
        data = json.loads(path.read_text())
        if not isinstance(data, list) or not data:
            raise ValueError()
        return [ModelSpec.model_validate(item) for item in data]
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid model catalog file") from None


def validate_upstream_catalog(settings, models: list[dict]):
    upstream = {m["id"]: m for m in models}
    for configured in settings.models:
        model = upstream.get(settings.upstream_for(configured))
        if not model:
            raise GatewayError("configured_model_missing")
        # Standard /models guarantees IDs, not context limits or tool capabilities.
        if settings.provider_mode == "openai-compatible" and settings.provider_catalog_validation == "ids":
            continue
        if configured.context_length > model.get("context_length", 0):
            raise GatewayError("configured_context_exceeds_provider")
        if configured.max_output_tokens > (model.get("top_provider", {}).get("max_completion_tokens") or 0):
            raise GatewayError("configured_output_unverified")
        if configured.tools and not {"tools", "tool_choice"} <= set(model.get("supported_parameters", [])):
            raise GatewayError("configured_tool_support_unverified")
