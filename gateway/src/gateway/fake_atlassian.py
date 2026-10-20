"""Synthetic Data Center APIs. Never deploy alongside production credentials."""
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

app = FastAPI(title="FAKE Jira / Confluence")


@app.middleware("http")
async def authenticate(request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    system = request.url.path.split("/")[1]
    if request.headers.get("authorization") != f"Bearer fake-{system}-pat":
        return JSONResponse({"error": "invalid synthetic PAT"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "FAKE"}


@app.get("/jira/rest/api/2/myself")
async def myself():
    return {"name": "fake-developer", "key": "FAKE", "active": True}


def issue(key):
    return {"key": key, "fields": {"summary": "FAKE: подключить Y.AI MCP", "description": "Синтетическая задача тестового стенда.",
        "status": {"name": "In Progress"}, "assignee": {"displayName": "Fake Developer"}}}


@app.get("/jira/rest/api/2/search")
async def search_issues(jql: str, maxResults: int = 10):
    return {"issues": [issue("DEMO-1")][:maxResults], "total": 1}


@app.get("/jira/rest/api/2/issue/{key}")
async def get_issue(key: str):
    return issue(key)


@app.get("/confluence/rest/api/user/current")
async def current_user():
    return {"type": "known", "username": "fake-developer", "userKey": "FAKE"}


def page(ident):
    return {"id": ident, "type": "page", "title": "FAKE: Y.AI Gateway", "space": {"key": "DEMO"},
            "version": {"number": 1}, "body": {"storage": {"value": "<p>Синтетическая документация для MCP.</p>"}}}


@app.get("/confluence/rest/api/content/search")
async def search_pages(cql: str, limit: int = 10):
    return {"results": [page("1001")][:limit], "size": 1}


@app.get("/confluence/rest/api/content/{ident}")
async def get_page(ident: str, authorization: str | None = Header(default=None)):
    return page(ident)
