import json

import pytest
from pydantic import ValidationError

from gateway.catalog import read_catalog
from gateway.config import Settings
from gateway.errors import GatewayError
from gateway.inference import Chat, Inference


IDS = ["deepseek/deepseek-v4.1-flash-1017", "qwen/qwen3.6-35b-a3b"]


def test_tool_history_preserves_provider_reasoning(settings):
    details=[{"type":"reasoning.encrypted","data":"SYNTHETIC_FIXTURE","index":0}]
    request=Chat(model="company-coder",messages=[{"role":"assistant","content":None,
        "reasoning":"Synthetic reasoning fixture", "reasoning_details":details, "refusal":None,
        "tool_calls":[{"id":"call_fixture","type":"function","function":{"name":"echo","arguments":"{}"}}]},
        {"role":"tool","tool_call_id":"call_fixture","content":"OK"}],max_tokens=16)
    body,_=Inference(settings,None,None).prepare(request,{"aliases":["company-coder"],"providers":["fake"]})
    assert body["messages"][0]["reasoning_details"]==details
    assert body["messages"][0]["reasoning"]=="Synthetic reasoning fixture"


@pytest.mark.parametrize("mode", ["fake", "openrouter-managed", "openai-compatible"])
def test_selected_model_routing_limits_and_policy(settings, mode):
    s = settings.model_copy(update={"model_catalog": read_catalog("builtin:y-ai"), "provider_mode": mode})
    inference = Inference(s, None, None)
    policy = {"aliases": IDS, "providers": [mode]}
    for model in s.models:
        request = Chat(model=model.id, messages=[{"role": "user", "content": "Synthetic test"}], max_tokens=64)
        body, _ = inference.prepare(request, policy)
        assert body["model"] == ("fake/tool-coder" if mode == "fake" else model.id)
        assert model.context_length == s.model_context
        assert model.max_output_tokens == s.model_max_output
        request.max_tokens = model.max_output_tokens + 1
        with pytest.raises(GatewayError, match="output_limit_exceeded"):
            inference.prepare(request, policy)
    policy["aliases"] = IDS[:1]
    with pytest.raises(GatewayError, match="model_not_allowed"):
        inference.prepare(Chat(model=IDS[1], messages=[{"role": "user", "content": "x"}]), policy)
    assert [m.id for m in s.allowed_models(policy)] == IDS[:1]
    policy["aliases"] += ["unconfigured/model"]
    with pytest.raises(GatewayError, match="model_not_configured"):
        inference.prepare(Chat(model="unconfigured/model", messages=[{"role": "user", "content": "x"}]), policy)
    policy["providers"] = ["another-provider"]
    assert s.allowed_models(policy) == []


def test_catalog_validation_and_per_model_capabilities(settings, tmp_path):
    catalog = read_catalog("builtin:y-ai")
    catalog[1].tools = False
    catalog[1].context_length, catalog[1].max_output_tokens = 8192, 128
    s = settings.model_copy(update={"model_catalog": catalog})
    model = s.models[1]
    assert (model.context_length, model.max_output_tokens, model.tools) == (8192, 128, False)
    with pytest.raises(GatewayError, match="tools_not_supported"):
        Inference(s, None, None).prepare(Chat(model=model.id, messages=[{"role": "user", "content": "x"}],
            tool_choice="auto"), {"aliases": IDS, "providers": ["fake"]})
    with pytest.raises(ValidationError, match="duplicate model IDs"):
        Settings(_env_file=None, **{**settings.model_dump(), "model_catalog": [catalog[0], catalog[0]]})
    file = tmp_path / "catalog.json"
    file.write_text(json.dumps({"unexpected": "shape"}))
    with pytest.raises(ValueError, match="invalid model catalog file"):
        read_catalog(str(file))
