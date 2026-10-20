"""Local test-only browser submission helper. Synthetic PATs or skip, never real PATs."""
import asyncio
from urllib.parse import urlsplit

import httpx


async def complete_setup(url, fake_pats=False):
    parsed = urlsplit(url)
    if parsed.hostname not in {"localhost", "127.0.0.1"} or not parsed.path.endswith("/auth/mcp/setup"):
        raise RuntimeError("MCP test setup must use loopback")
    endpoint = url.split("#")[0] + "/session"
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {"Authorization": "Bearer " + parsed.fragment, "Origin": origin}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as c:
        info = await c.get(endpoint, headers=headers)
        for _ in range(30):
            if info.status_code != 404 or info.json().get("error", {}).get("code") != "mcp_setup_pending":
                break
            await asyncio.sleep(1)
            info = await c.get(endpoint, headers=headers)
        info.raise_for_status()
        body = {}
        for server in info.json()["servers"]:
            if server["id"] not in {"jira", "confluence"}:
                raise RuntimeError("Unknown MCP system")
            body[server["id"]] = {"action": "replace", "token": "fake-" + server["id"] + "-pat"} if fake_pats else {"action": "keep"}
        result = await c.post(endpoint, headers=headers, json=body)
        result.raise_for_status()
