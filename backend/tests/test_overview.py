from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_overview() -> None:
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert "leads" in response.json()
