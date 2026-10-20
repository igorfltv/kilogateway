"""Bounded, read-only Data Center connectors. PATs never cross the gateway boundary."""
import asyncio
import json
import re

import httpx

from gateway.errors import GatewayError


TOOLS = {
    "jira": [
        {"name": "search_issues", "description": "Search Jira issues using JQL. Read-only.",
         "inputSchema": {"type": "object", "properties": {"jql": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["jql"], "additionalProperties": False}},
        {"name": "get_issue", "description": "Read a Jira issue by key.",
         "inputSchema": {"type": "object", "properties": {"key": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]*-[0-9]+$"}},
            "required": ["key"], "additionalProperties": False}},
    ],
    "confluence": [
        {"name": "search_pages", "description": "Search Confluence pages using CQL. Read-only.",
         "inputSchema": {"type": "object", "properties": {"cql": {"type": "string", "minLength": 1, "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["cql"], "additionalProperties": False}},
        {"name": "get_page", "description": "Read a Confluence page including its storage-format body.",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[0-9]{1,20}$"}},
            "required": ["id"], "additionalProperties": False}},
    ],
}
for tools in TOOLS.values():
    for tool in tools:
        tool["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}


class Atlassian:
    def __init__(self, settings, client):
        self.s, self.client = settings, client

    def endpoint(self, system):
        return getattr(self.s, f"{system}_base_url") if system in TOOLS else ""

    async def get(self, system, token, path, params=None):
        base = self.endpoint(system)
        if not base:
            raise GatewayError("mcp_not_configured", 404)
        try:
            async with self.client.stream("GET", base + path, params=params,
                    headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Cookie": ""},
                    timeout=self.s.mcp_timeout_seconds, follow_redirects=False) as response:
                if response.status_code in {401, 403}:
                    raise GatewayError("mcp_pat_rejected", 422)
                if response.status_code == 404:
                    raise GatewayError("mcp_resource_not_found", 404)
                if response.status_code == 429:
                    raise GatewayError("mcp_upstream_rate_limited", 429, 30)
                if response.status_code != 200:
                    raise GatewayError("mcp_upstream_unavailable", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.s.mcp_response_limit:
                        raise GatewayError("mcp_response_too_large", 502)
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (httpx.HTTPError, ValueError, TypeError):
            raise GatewayError("mcp_upstream_unavailable", 502) from None

    async def validate(self, system, token):
        path = "/rest/api/2/myself" if system == "jira" else "/rest/api/user/current"
        try:
            async with asyncio.timeout(self.s.mcp_timeout_seconds):
                data = await self.get(system, token, path)
        except TimeoutError:
            raise GatewayError("mcp_upstream_unavailable", 502) from None
        if system == "jira":
            valid = data.get("active") is not False and (data.get("name") or data.get("key"))
        else:
            valid = data.get("type") != "anonymous" and (data.get("username") or data.get("userKey"))
        if not valid:
            raise GatewayError("mcp_pat_rejected", 422)

    async def call(self, system, name, args, token):
        if not isinstance(args, dict):
            raise GatewayError("mcp_invalid_arguments", 400)
        if name in {"search_issues", "search_pages"}:
            query_key = "jql" if system == "jira" else "cql"
            query, limit = args.get(query_key), args.get("limit", 10)
            if (set(args) - {query_key, "limit"} or not isinstance(query, str) or not 1 <= len(query) <= 2000
                    or type(limit) is not int or not 1 <= limit <= 20):
                raise GatewayError("mcp_invalid_arguments", 400)
            path = "/rest/api/2/search" if system == "jira" else "/rest/api/content/search"
            params = {query_key: query, "maxResults" if system == "jira" else "limit": limit}
            if system == "jira":
                params["fields"] = "summary,status,assignee,description"
            result = await self.get(system, token, path, params)
            key = "issues" if system == "jira" else "results"
            return {key: [self.project(system, item) for item in result.get(key, [])[:limit]],
                    "total": result.get("total", result.get("size"))}
        if system == "jira" and name == "get_issue":
            key = args.get("key")
            if set(args) != {"key"} or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,60}-[0-9]{1,20}", key):
                raise GatewayError("mcp_invalid_arguments", 400)
            data = await self.get(system, token, "/rest/api/2/issue/" + key,
                                  {"fields": "summary,status,assignee,description"})
        elif system == "confluence" and name == "get_page":
            ident = args.get("id")
            if set(args) != {"id"} or not isinstance(ident, str) or not re.fullmatch(r"[0-9]{1,20}", ident):
                raise GatewayError("mcp_invalid_arguments", 400)
            data = await self.get(system, token, "/rest/api/content/" + ident, {"expand": "body.storage,version,space"})
        else:
            raise GatewayError("mcp_tool_not_found", 404)
        return self.project(system, data)

    @staticmethod
    def project(system, data):
        if system == "jira":
            f = data.get("fields") or {}
            return {"key": data.get("key"), "summary": f.get("summary"), "description": f.get("description"),
                    "status": (f.get("status") or {}).get("name"),
                    "assignee": (f.get("assignee") or {}).get("displayName")}
        return {"id": data.get("id"), "title": data.get("title"), "type": data.get("type"),
                "body": ((data.get("body") or {}).get("storage") or {}).get("value"),
                "version": (data.get("version") or {}).get("number"), "space": (data.get("space") or {}).get("key")}
