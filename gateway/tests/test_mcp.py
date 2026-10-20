import httpx
import pytest

from gateway.atlassian import Atlassian
from gateway.config import Settings
from gateway.errors import GatewayError


async def test_pat_never_follows_redirect_and_responses_are_bounded(settings):
    settings.jira_base_url = "http://localhost:18091/jira"
    calls = []

    async def upstream(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/capture"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as c:
        connector = Atlassian(settings, c)
        with pytest.raises(GatewayError, match="mcp_upstream_unavailable"):
            await connector.validate("jira", "SYNTHETIC_PAT")
    assert len(calls) == 1
    assert str(calls[0].url) == settings.jira_base_url + "/rest/api/2/myself"
    assert calls[0].headers["Authorization"] == "Bearer SYNTHETIC_PAT"
    settings.mcp_response_limit = 32
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 33))) as c:
        with pytest.raises(GatewayError, match="mcp_response_too_large"):
            await Atlassian(settings, c).validate("jira", "SYNTHETIC_PAT")


async def test_anonymous_confluence_is_not_a_valid_pat(settings):
    settings.confluence_base_url = "http://localhost:18091/confluence"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            json={"type": "anonymous", "username": "anonymous"}))) as c:
        with pytest.raises(GatewayError, match="mcp_pat_rejected"):
            await Atlassian(settings, c).validate("confluence", "SYNTHETIC_PAT")


def test_endpoint_change_requires_trusted_operator_config(settings):
    public = settings.model_dump()
    for value in ("https://host/path/../other", "https://host/%2fsecret", "https://u:p@host/", "https://host/?q=1", "http://jira.internal"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{**public, "jira_base_url": value})
    Settings(_env_file=None, **{**public, "jira_base_url": "http://fake-atlassian:8091/jira"})
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{**public, "jira_base_url": "http://fake-atlassian:8092/jira"})
