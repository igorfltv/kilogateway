"""Test helper: submit the real Keycloak browser forms for a local dev user.

Does not enable password grants or bypass Device Authorization. Never use with production credentials.
"""
import asyncio
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "form":
            self.current = {"action": d.get("action", ""), "inputs": {}, "id": d.get("id", "")}
            self.forms.append(self.current)
        if tag in {"input", "button"} and self.current is not None and d.get("name"):
            if d.get("type") != "submit" or d.get("name") in {"accept", "confirm"}:
                self.current["inputs"][d["name"]] = d.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


async def approve(url, username, password):
    if urlparse(url).hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Only local development browser login is allowed")
    async def localhost_cookies(response):
        # Browsers treat localhost as a secure context, including Secure cookies on HTTP.
        # http.cookiejar does not. Emulate that browser behavior only for this loopback test helper.
        for cookie in c.cookies.jar:
            if cookie.domain in {"localhost.local", "localhost", "127.0.0.1"}:
                cookie.secure = False

    async with httpx.AsyncClient(follow_redirects=True, trust_env=False, timeout=15,
                                event_hooks={"response": [localhost_cookies]}) as c:
        r = await c.get(url)
        for _ in range(6):
            forms = Forms(r.text).forms
            login = next((x for x in forms if "username" in x["inputs"]), None)
            consent = next((x for x in forms if "accept" in x["inputs"] or x["id"] == "kc-user-verify-device-form"), None)
            form = login or consent
            if not form:
                if "success" in r.text.lower() or "granted" in r.text.lower():
                    return
                raise RuntimeError("Unexpected Keycloak browser page")
            data = form["inputs"]
            if login:
                data.update(username=username, password=password)
            else:
                data["accept"] = "Yes"
            action = urljoin(str(r.url), form["action"])
            if urlparse(action).netloc != urlparse(url).netloc:
                raise RuntimeError("Untrusted browser form action")
            r = await c.post(action, data=data)
        raise RuntimeError("Keycloak browser form limit reached")


async def access_token(issuer, username, password):
    async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
        d = (await c.get(issuer + "/.well-known/openid-configuration")).json()
        r = await c.post(d["device_authorization_endpoint"], data={"client_id": "kilo-cli", "scope": "openid"})
        r.raise_for_status()
        flow = r.json()
        await approve(flow["verification_uri_complete"], username, password)
        for _ in range(30):
            await asyncio.sleep(flow.get("interval", 5))
            r = await c.post(d["token_endpoint"], data={"client_id": "kilo-cli", "device_code": flow["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if r.status_code == 200:
                return r.json()["access_token"]
            if r.json().get("error") != "authorization_pending":
                raise RuntimeError("Device token request failed")
    raise RuntimeError("Device token deadline reached")
