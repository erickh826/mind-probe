"""Deploy-readiness wiring: env vars, prod compose, and frontend CSP."""

from __future__ import annotations

import json
import sys
from collections import namedtuple
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import capacity, models, storage
from app.main import app
from app.models import Base

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
Usage = namedtuple("Usage", "total used free")
CSP_HOSTS = (
    "https://*.sslip.io",
    "wss://*.sslip.io",
    "https://*.r2.dev",
    "https://*.r2.cloudflarestorage.com",
)


@pytest.fixture()
def api(monkeypatch):
    monkeypatch.delenv("MINDPROBE_PRESIGN_TTL_SECONDS", raising=False)
    monkeypatch.delenv("MINDPROBE_DISK_WARN_PCT", raising=False)
    monkeypatch.delenv("MINDPROBE_DISK_MIN_FREE_GB", raising=False)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[models.get_db] = override
    yield TestClient(app)
    app.dependency_overrides.pop(models.get_db, None)
    engine.dispose()


def _usage(free_gib: int, used_gib: int, total_gib: int | None = None):
    total = total_gib if total_gib is not None else free_gib + used_gib
    return lambda path: Usage(total * GIB, used_gib * GIB, free_gib * GIB)


def test_presign_ttl_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDPROBE_WS_TOKEN_SECRET", "signing-secret")
    monkeypatch.setattr(storage.time, "time", lambda: 1_700_000_000)
    store = storage.LocalStorage(tmp_path)
    monkeypatch.delenv("MINDPROBE_PRESIGN_TTL_SECONDS", raising=False)
    assert "expires=1700000900" in store.presign_get(storage.recording_key("TAKE"))

    monkeypatch.setenv("MINDPROBE_PRESIGN_TTL_SECONDS", "120")
    signed = store.presign_get(storage.recording_key("TAKE"))
    assert "expires=1700000120" in signed
    expires = int(signed.split("expires=")[1].split("&", 1)[0])
    signature = signed.split("signature=")[1]
    assert storage.valid_local_signature(storage.recording_key("TAKE"), expires, signature)

    for raw in ("forever", "0", "86401"):
        monkeypatch.setenv("MINDPROBE_PRESIGN_TTL_SECONDS", raw)
        assert storage.presign_ttl_seconds() == 900


def test_s3_presign_and_case_keys_honor_bucket_and_ttl(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDPROBE_PRESIGN_TTL_SECONDS", "120")

    class Fake:
        def __init__(self):
            self.uploads: list[tuple[str, str]] = []
            self.signed_bucket = ""

        def upload_file(self, filename, bucket, key, **kwargs):
            self.uploads.append((bucket, key))

        def generate_presigned_url(self, action, **kwargs):
            assert action == "get_object"
            assert kwargs["ExpiresIn"] == 120
            self.signed_bucket = kwargs["Params"]["Bucket"]
            return "https://example.invalid/signed"

        def delete_object(self, **kwargs):
            self.deleted = (kwargs["Bucket"], kwargs["Key"])

    fake = Fake()
    store = storage.S3Storage("recordings", client=fake, media_bucket="mindprobe-media")
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"clip")
    digest = storage.file_sha256(source)
    store.put_file("cases/CASE001/media/clip.mp4", source, digest)
    store.put_file(storage.recording_key("TAKE"), source, digest)
    assert fake.uploads == [
        ("mindprobe-media", "cases/CASE001/media/clip.mp4"),
        ("recordings", "sessions/TAKE/student.mp4"),
    ]
    assert store.presign_get(storage.recording_key("TAKE")) == "https://example.invalid/signed"
    assert fake.signed_bucket == "recordings"
    store.delete("cases/CASE001/media/clip.mp4")
    assert fake.deleted == ("mindprobe-media", "cases/CASE001/media/clip.mp4")

    plain = storage.S3Storage("recordings", client=Fake())
    plain.put_file("cases/CASE001/media/clip.mp4", source, digest)
    assert plain.client.uploads[0][0] == "recordings"


def test_custom_s3_endpoint_uses_path_style_addressing(monkeypatch):
    captured: dict = {}

    def client(service, **kwargs):
        captured["service"] = service
        captured["kwargs"] = kwargs
        return object()

    fake = ModuleType("boto3")
    fake.client = client
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setenv("MINDPROBE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("MINDPROBE_RECORDINGS_BUCKET", "recs")
    monkeypatch.setenv("MINDPROBE_MEDIA_BUCKET", " mindprobe-media ")
    monkeypatch.setenv("MINDPROBE_S3_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("MINDPROBE_S3_REGION", "auto")
    monkeypatch.setenv("MINDPROBE_S3_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("MINDPROBE_S3_SECRET_ACCESS_KEY", "test-secret")

    store = storage.get_storage()
    assert captured["service"] == "s3"
    assert store.bucket == "recs"
    assert store.media_bucket == "mindprobe-media"
    assert captured["kwargs"]["endpoint_url"] == "https://example.invalid"
    assert captured["kwargs"]["config"].s3["addressing_style"] == "path"

    monkeypatch.delenv("MINDPROBE_S3_ENDPOINT")
    storage.S3Storage("recs")
    assert captured["kwargs"]["endpoint_url"] is None
    assert "s3" not in (captured["kwargs"]["config"].s3 or {})


def test_disk_levels_and_session_refusal(api, monkeypatch):
    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(30, 70, 100))
    assert api.get("/health").json()["disk"]["level"] == "ok"

    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(29, 71, 100))
    assert api.get("/health").json()["disk"]["level"] == "warn"
    warned = api.post("/sessions", json={"session_id": "S-DISK-WARN", "case_id": "CASE001"})
    assert warned.status_code == 201

    monkeypatch.setattr(capacity.shutil, "disk_usage", _usage(4, 96, 100))
    body = api.get("/health").json()
    assert body["status"] == "ok"
    assert body["disk"]["level"] == "critical"
    refused = api.post("/sessions", json={"session_id": "S-DISK-FULL", "case_id": "CASE001"})
    assert refused.status_code == 507
    assert refused.json()["detail"] == "disk_full"

    monkeypatch.setenv("MINDPROBE_DISK_MIN_FREE_GB", "1")
    monkeypatch.setenv("MINDPROBE_DISK_WARN_PCT", "99")
    allowed = api.post("/sessions", json={"session_id": "S-DISK-OK", "case_id": "CASE001"})
    assert allowed.status_code == 201

    monkeypatch.setenv("MINDPROBE_DISK_WARN_PCT", "nope")
    monkeypatch.setenv("MINDPROBE_DISK_MIN_FREE_GB", "-3")
    health = api.get("/health").json()["disk"]
    assert health["warn_pct"] == 70
    assert health["min_free_gb"] == 5

    def unavailable(path):
        raise OSError("stat failed")

    monkeypatch.setattr(capacity.shutil, "disk_usage", unavailable)
    assert api.get("/health").json()["disk"]["level"] == "unknown"
    unknown = api.post("/sessions", json={"session_id": "S-DISK-UNKNOWN", "case_id": "CASE001"})
    assert unknown.status_code == 201


def test_prod_compose_proxies_without_publishing_the_api():
    dev = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "MINDPROBE_ENV: \"development\"" in dev
    assert '"8000:8000"' in dev
    grace = 'MINDPROBE_UPLOAD_GRACE_SYNTHESIZE_AFTER: "${MINDPROBE_UPLOAD_GRACE_SYNTHESIZE_AFTER:-}"'
    assert grace in dev
    assert grace in prod
    assert "8000:8000" not in prod
    assert "--proxy-headers" in prod
    assert '"${FORWARDED_ALLOW_IPS:-*}"' in prod
    assert "--workers" in prod
    assert "caddy:2-alpine" in prod
    assert "reverse_proxy server:8000" in caddy
    assert "max_size 20MB" in caddy
    assert "request>headers>Authorization delete" in caddy
    assert "request>headers>Sec-Websocket-Protocol delete" in caddy
    assert "example-account" not in prod
    assert "AKIA" not in prod


def test_vercel_csp_and_monorepo_layout():
    apps = {
        "student": "camera=(self), microphone=(self)",
        "wizard": "camera=(), microphone=()",
        "teacher": "camera=(), microphone=()",
    }
    for name, permissions in apps.items():
        directory = ROOT / "apps" / name
        spec = json.loads((directory / "vercel.json").read_text(encoding="utf-8"))
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        assert package["dependencies"]["@mind-probe/shared"] == "file:../../packages/shared"
        assert spec["rewrites"] == [{"source": "/(.*)", "destination": "/index.html"}]
        headers = {item["key"]: item["value"] for item in spec["headers"][0]["headers"]}
        policy = headers["Content-Security-Policy"]
        assert "script-src 'self'" in policy
        assert "unsafe-eval" not in policy
        assert "unsafe-inline" not in policy.split("script-src", 1)[1].split(";", 1)[0]
        assert "object-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        for host in CSP_HOSTS:
            assert host in policy
        assert headers["Permissions-Policy"].startswith(permissions)
        assert "AKIA" not in policy


def test_op_vercel_csp_is_api_only():
    """The OP page holds the operator token, so it gets the narrowest policy."""
    directory = ROOT / "apps" / "op"
    spec = json.loads((directory / "vercel.json").read_text(encoding="utf-8"))
    assert spec["rewrites"] == [{"source": "/(.*)", "destination": "/index.html"}]
    headers = {item["key"]: item["value"] for item in spec["headers"][0]["headers"]}
    policy = headers["Content-Security-Policy"]
    directives = dict(
        part.strip().split(" ", 1) for part in policy.split(";") if part.strip()
    )
    assert directives["script-src"] == "'self'"
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert directives["connect-src"] == "'self' https://*.sslip.io"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Permissions-Policy"].startswith("camera=(), microphone=()")
