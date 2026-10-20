import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.errors import GatewayError
from gateway.oidc import Identity


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(settings, key, **changes):
    data = {"iss": settings.oidc_issuer, "sub": "alice", "aud": settings.oidc_audience,
            "iat": int(time.time())-5, "exp": int(time.time())+60, "typ": "Bearer", "azp": "kilo-cli",
            "realm_access": {"roles": ["invoke", "issue-credential"]}}
    data.update(changes)
    return jwt.encode(data, key, algorithm="RS256", headers={"kid": "test"})


def identity(settings, key):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test", use="sig")

    def handler(r):
        if "well-known" in r.url.path:
            return httpx.Response(200, json={"issuer": settings.oidc_issuer,
                "jwks_uri": settings.oidc_issuer+"/certs", "token_endpoint": settings.oidc_issuer+"/token"})
        return httpx.Response(200, json={"keys": [jwk]})
    return Identity(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_valid_access_token(settings, key):
    i = identity(settings, key)
    assert (await i.verify(token(settings, key)))["sub"] == "alice"
    await i.client.aclose()


@pytest.mark.parametrize("changes", [{"iss": "https://wrong"}, {"aud": "wrong"}, {"exp": 1},
    {"typ": "ID"}, {"sub": ""}, {"azp": "untrusted-client"}, {"realm_access": {"roles": []}}])
async def test_invalid_claims(settings, key, changes):
    i = identity(settings, key)
    with pytest.raises(GatewayError):
        await i.verify(token(settings, key, **changes))
    await i.client.aclose()


async def test_bad_signature(settings, key):
    i = identity(settings, key)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(GatewayError, match="invalid_access_token"):
        await i.verify(token(settings, attacker))
    await i.client.aclose()


async def test_jwks_rotation(settings, key):
    i = identity(settings, key)
    await i.verify(token(settings, key))
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = identity(settings, rotated)
    await i.client.aclose()
    i.client = other.client
    i.keys_at = 0
    assert (await i.verify(token(settings, rotated)))["sub"] == "alice"
    await i.client.aclose()
