from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def rsa_keys() -> tuple[str, str]:
    """(private_pem, public_pem) generated for the test run; never a real key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


class Recorder:
    """httpx.MockTransport wrapper that records every request it serves."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def reply(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body if body is not None else {}), headers=headers)
