import base64
import hashlib
import hmac
import json
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretStore(Protocol):
    def seal(self, plaintext: str, binding: str) -> str: ...
    def open(self, envelope: str, binding: str) -> str: ...


class AESSecretStore:
    """External key; versioned envelope. Bind each ciphertext to owner/provider/purpose."""
    def __init__(self, key: str, version: str):
        raw = base64.b64decode(key, validate=True)
        if len(raw) != 32:
            raise ValueError("encryption key must decode to 32 bytes")
        self.aes, self.version = AESGCM(raw), version

    def seal(self, plaintext: str, binding: str) -> str:
        nonce = os.urandom(12)
        aad = f"{self.version}:{binding}".encode()
        ciphertext = self.aes.encrypt(nonce, plaintext.encode(), aad)
        return json.dumps({"v": self.version, "n": base64.b64encode(nonce).decode(),
                           "c": base64.b64encode(ciphertext).decode()})

    def open(self, envelope: str, binding: str) -> str:
        obj = json.loads(envelope)
        if obj["v"] != self.version:
            raise ValueError("unknown encryption key version")
        return self.aes.decrypt(base64.b64decode(obj["n"]), base64.b64decode(obj["c"]),
                                f"{self.version}:{binding}".encode()).decode()


def digest(value: str, key: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
