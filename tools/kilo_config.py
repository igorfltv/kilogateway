import argparse
import asyncio
import json
from pathlib import Path

from gateway.config import Settings
from tools.validate_model import main as validate


def build_config(s, npm=None):
    oidc = {"oidcAllowedOrigins": s.oidc_allowed_origins,
            **{k:v for k,v in {"discoveryUrl":s.oidc_discovery_url,"deviceUrl":s.oidc_device_url,
                              "tokenUrl":s.oidc_token_url}.items() if v}}
    plugin = npm or (Path(__file__).resolve().parents[1] / "plugin/dist/index.js").as_uri()
    config = {"$schema":"https://kilo.ai/config.json", "plugin":[[plugin,{
        "issuer":s.oidc_issuer,"gateway":s.gateway_public_url,"clientId":s.oidc_client_id,"dev":s.profile=="dev","openBrowser":True,
        **oidc}]],
        "provider":{"y.ai":{"name":"Y.AI", "npm":"@ai-sdk/openai-compatible@2.0.48",
            "options":{"baseURL":s.gateway_public_url+"/v1"},"models":{m.id:{
                "name":("FAKE: " if s.provider_mode=="fake" else "")+m.name,"tool_call":m.tools,
                "modalities":{"input":["text"],"output":["text"]},
                "limit":{"context":m.context_length,"output":m.max_output_tokens}} for m in s.models}}},
        "model":"y.ai/"+s.models[0].id}
    return config


async def main():
    parser = argparse.ArgumentParser(description="Validate selected upstream model, then generate explicit Kilo metadata")
    parser.add_argument("--output", required=True)
    parser.add_argument("--npm", help="Exact already-published internal plugin package@version; default is local file")
    parser.add_argument("--force", action="store_true", help="Replace an explicitly selected public export file")
    args = parser.parse_args()
    await validate()
    s = Settings()
    config = build_config(s, args.npm)
    p = Path(args.output)
    if p.exists() and not args.force:
        raise SystemExit("Refusing to overwrite existing Kilo config")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config,indent=2)+"\n")
    print("Created validated Kilo config:", p)


if __name__ == "__main__":
    asyncio.run(main())
