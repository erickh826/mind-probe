"""Health-check endpoint test (spec §5, design §14)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok(monkeypatch):
    monkeypatch.delenv("MINDPROBE_DISK_WARN_PCT", raising=False)
    monkeypatch.delenv("MINDPROBE_DISK_MIN_FREE_GB", raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "mind-probe-server"
    assert body["disk"]["level"] in {"ok", "warn", "critical", "unknown"}
    assert body["disk"]["warn_pct"] == 70
    assert body["disk"]["min_free_gb"] == 5
