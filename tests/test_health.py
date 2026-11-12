from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_root_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Coldbrew API"


def test_database_routes_say_503_when_not_configured(monkeypatch) -> None:
    from config import get_settings
    from database import get_engine

    from dependencies import get_current_user
    from tests.conftest import ADMIN

    # Signed in (this test doesn't rely on another test's leftover sign-in), so the database is what's missing.
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: ADMIN)
    monkeypatch.setattr(get_settings(), "database_url", None)
    get_engine.cache_clear()
    response = client.get("/api/v1/icps")
    assert response.status_code == 503
    get_engine.cache_clear()
