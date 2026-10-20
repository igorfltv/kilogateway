"""Export native OpenCode v2 configuration using the same gateway/model policy."""

import argparse
import asyncio
import json
from pathlib import Path

from gateway.config import Settings
from tools.kilo_config import build_config as kilo_config
from tools.validate_model import main as validate


def build_config(settings, package=None):
    kilo = kilo_config(settings)
    provider = kilo["provider"]["y.ai"]
    return {
        "$schema": "https://opencode.ai/config.json",
        "plugins": [
            {
                "package": package
                or (Path(__file__).resolve().parents[1] / "plugin/opencode").as_uri(),
                "options": kilo["plugin"][0][1],
            }
        ],
        "providers": {
            "y.ai": {
                "name": "Y.AI",
                "package": "@opencode/ai/providers/openai-compatible",
                "env": [],
                "settings": {"baseURL": settings.gateway_public_url + "/v1"},
                "models": {
                    key: {
                        "name": value["name"],
                        "limit": value["limit"],
                        "compatibility": {
                            "supportsStore": False,
                            "supportsPromptCacheKey": False,
                            "maxTokensField": "max_tokens",
                        },
                        "capabilities": {
                            "tools": value["tool_call"],
                            **value["modalities"],
                        },
                    }
                    for key, value in provider["models"].items()
                },
            }
        },
        "model": kilo["model"],
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--package",
        help="Published package entrypoint for OpenCode v2; default is local file",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    await validate()
    destination = Path(args.output)
    if destination.exists() and not args.force:
        raise SystemExit("Refusing to replace existing configuration without --force")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(build_config(Settings(), args.package), indent=2) + "\n"
    )
    print("Created validated OpenCode v2 config:", destination)


if __name__ == "__main__":
    asyncio.run(main())
