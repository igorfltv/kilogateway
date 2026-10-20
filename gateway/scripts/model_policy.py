"""Explicitly sync default model/provider permissions, preserving budgets and credentials."""
import argparse
import asyncio
import json

from gateway.broker import uid
from gateway.config import Settings
from gateway.db import DB, audit, one, run


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Update default model permissions; otherwise dry-run")
    parser.add_argument("--switch-provider", action="store_true", help="Explicitly replace default provider grant with the selected runtime provider")
    args = parser.parse_args()
    s = Settings()
    db = DB(s.database_url.get_secret_value(), s.database_schema)
    try:
        async with db.tx() as c:
            policy = await one(c, "SELECT * FROM policies WHERE id='default' FOR UPDATE")
            if not policy or (s.provider_mode not in policy["providers"] and not args.switch_provider):
                raise RuntimeError("default policy is missing or uses another provider")
            models = [m.id for m in s.models]
            providers = [s.provider_mode] if args.switch_provider else list(policy["providers"])
            changed = list(policy["aliases"]) != models or list(policy["providers"]) != providers
            if args.apply and changed:
                await run(c, "UPDATE policies SET aliases=:a,providers=:p WHERE id='default'", a=models,p=providers)
                await audit(c, "admin", "policy.provider" if args.switch_provider else "policy.models", "default", "success", uid("policy_"))
            print(json.dumps({"status": "applied" if args.apply else "dry-run", "policy": "default",
                              "previous_providers": list(policy["providers"]), "providers": providers,
                              "previous_models": list(policy["aliases"]), "models": models, "changed": changed}))
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("Model policy update failed; check database/provider configuration (secrets suppressed)") from None
