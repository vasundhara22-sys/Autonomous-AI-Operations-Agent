from fastapi.testclient import TestClient

from app import db
from app.api import create_app
from app.auth import create_user
from app.models import Role
from app.seed import seed_reference_data
from tests.conftest import SAMPLE_REQUEST


def _client(settings):
    app = create_app(settings)
    db.configure(settings.database_url)
    with db.session_factory()() as s:
        seed_reference_data(s)
        analyst = create_user(s, "A", Role.ANALYST)
        approver = create_user(s, "B", Role.APPROVER)
    return TestClient(app), analyst, approver


def test_health_needs_no_key(settings):
    client, _, _ = _client(settings)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_missing_key_is_rejected(settings):
    client, _, _ = _client(settings)
    assert client.get("/api/workflows").status_code == 401


def test_bad_key_is_rejected(settings):
    client, _, _ = _client(settings)
    assert client.get("/api/workflows", headers={"X-API-Key": "nope"}).status_code == 401


def test_workflow_and_approval_over_http(settings):
    client, analyst, approver = _client(settings)
    a_headers = {"X-API-Key": analyst}
    p_headers = {"X-API-Key": approver}

    created = client.post("/api/workflows", json={"request": SAMPLE_REQUEST}, headers=a_headers)
    assert created.status_code == 201
    wf = created.json()
    assert wf["status"] == "WAITING_FOR_APPROVAL"

    approval_id = wf["approvals"][0]["id"]
    forbidden = client.post(f"/api/approvals/{approval_id}/approve", headers=a_headers)
    assert forbidden.status_code == 403

    approved = client.post(f"/api/approvals/{approval_id}/approve", headers=p_headers)
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"


def test_errors_do_not_leak_tracebacks(settings):
    client, analyst, _ = _client(settings)
    response = client.get("/api/workflows/does-not-exist", headers={"X-API-Key": analyst})
    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_metrics_endpoint(settings):
    client, analyst, _ = _client(settings)
    response = client.get("/api/sales/metrics", params={"threshold": 0.8}, headers={"X-API-Key": analyst})
    assert response.status_code == 200
    body = response.json()
    assert body["period_week"] == "2026-11-09"
    assert len(body["regions"]) == 6
