from unittest.mock import Mock

from sqlalchemy.exc import OperationalError

from app.routers import health


def test_health_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_database_error(client, monkeypatch):
    engine = Mock()
    engine.connect.side_effect = OperationalError("SELECT 1", {}, Exception("private error"))
    monkeypatch.setattr(health, "get_engine", lambda: engine)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error"}
    assert "private error" not in response.text
    assert "SELECT" not in response.text
