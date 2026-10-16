"""Shared test setup for the ADR-0005 auth layer.

The pre-existing suite drives the API anonymously and through cookies. Rather
than rewriting every call site, an operator Bearer header is injected into the
few session-management requests that the suite makes without credentials.
Tests that probe authorization send their own ``Authorization``/``Cookie``
header, which this shim never overrides.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import os

os.environ.setdefault("MINDPROBE_WS_TOKEN_SECRET", "test-api-secret")
# Module-level TestClient() starts the app lifespan during collection, before
# fixtures run. The grace thread shares the suite's SQLite connection, so it
# has to be off before that import. Tests call apply_upload_grace_deadlines
# directly.
os.environ["MINDPROBE_UPLOAD_GRACE_SCHEDULER"] = "false"

OPERATOR_TOKEN = "test-operator-token-0123456789abcdef"

_OPERATOR_ROUTES = (
    ("POST", re.compile(r"^/sessions$")),
    ("GET", re.compile(r"^/sessions/[^/]+$")),
    ("GET", re.compile(r"^/sessions/[^/]+/events$")),
    ("POST", re.compile(r"^/sessions/[^/]+/end$")),
)


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("MINDPROBE_OPERATOR_TOKEN", OPERATOR_TOKEN)
    # The legacy suite authenticates with the role cookie; Bearer-only is
    # covered explicitly in test_auth_matrix.py.
    monkeypatch.setenv("MINDPROBE_AUTH_COOKIE", "true")
    # The minute grace loop shares SQLite with the suite. Tests call
    # apply_upload_grace_deadlines directly.
    monkeypatch.setenv("MINDPROBE_UPLOAD_GRACE_SCHEDULER", "false")


@pytest.fixture(autouse=True)
def _operator_for_session_management(monkeypatch):
    original = TestClient.request

    def request(self, method, url, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        lowered = {k.lower() for k in headers}
        path = str(url).split("?", 1)[0]
        if (
            "authorization" not in lowered
            and "cookie" not in lowered
            and "x-no-operator" not in lowered
            and not self.cookies
            and any(m == method.upper() and rx.match(path) for m, rx in _OPERATOR_ROUTES)
        ):
            headers["Authorization"] = f"Bearer {OPERATOR_TOKEN}"
            kwargs["headers"] = headers
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)
