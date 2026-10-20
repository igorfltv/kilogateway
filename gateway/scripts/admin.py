"""Server-side operator helper. No keys in argv or output."""
import argparse
import asyncio
import json

import httpx

from gateway.config import Settings
from gateway.db import DB, rows


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["list", "suspend", "rotate"])
    p.add_argument("id", nargs="?")
    args = p.parse_args()
    s = Settings()
    if args.action == "list":
        db = DB(s.database_url.get_secret_value(), s.database_schema)
        try:
            async with db.tx() as c:
                data = await rows(c, """SELECT p.id AS principal_id,p.status,pc.id AS provider_credential_id,
                    pc.state,pc.generation,pc.expires_at,p.provider_allocated FROM principals p
                    LEFT JOIN provider_credentials pc ON pc.principal_id=p.id ORDER BY p.id,pc.generation""")
                pending = await rows(c, """SELECT id,principal_id,state,label,created_at FROM provisioning_operations
                    WHERE state IN ('started','uncertain') ORDER BY created_at""")
            print(json.dumps({"principals_and_credentials":[dict(x) for x in data],
                              "pending_operations":[dict(x) for x in pending]},default=str,indent=2))
        finally:
            await db.engine.dispose()
        return
    if not args.id or not args.id.replace("_", "").isalnum():
        raise SystemExit("An exact public ID is required")
    path = f"/admin/users/{args.id}/suspend" if args.action == "suspend" else f"/admin/provider-credentials/{args.id}/rotate"
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=30) as c:
        r = await c.post(s.gateway_public_url + path, headers={"Authorization":"Bearer " + s.admin_token.get_secret_value()})
        if r.status_code != 200:
            raise SystemExit(f"Admin operation failed ({r.status_code}); no secrets printed")
        print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
