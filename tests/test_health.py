from fastapi.testclient import TestClient


def test_health_returns_exact_minimal_response(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    serialized = response.text.lower()
    assert "environment" not in serialized
    assert "database" not in serialized
    assert "python" not in serialized
    assert "path" not in serialized

