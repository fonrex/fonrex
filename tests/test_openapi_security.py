"""The OpenAPI document declares the API key so Swagger UI can send it."""

from fastapi.testclient import TestClient

from main import app


def test_openapi_declares_api_key_schemes():
    schema = TestClient(app).get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["ApiKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-KEY"}
    assert schemes["BearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert {"ApiKeyHeader": []} in schema["security"]
    assert {"BearerAuth": []} in schema["security"]


def test_openapi_still_lists_routes():
    schema = TestClient(app).get("/openapi.json").json()
    assert "/eod/{ticker}" in schema["paths"]


def test_docs_persist_authorization():
    html = TestClient(app).get("/docs").text
    assert '"persistAuthorization": true' in html
