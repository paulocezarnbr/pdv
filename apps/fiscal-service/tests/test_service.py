from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fiscal_service.app import create_app
from fiscal_service.models import FiscalIntent, FiscalResult
from fiscal_service.security import SecretError, SecretResolver
from fiscal_service.store import ResultStore


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0

    def authorize(self, _intent: FiscalIntent) -> FiscalResult:
        self.calls += 1
        return FiscalResult(status="authorized", code="100", reason="Autorizado",
                            access_key="3" * 44, protocol="123")


class BrokenEngine:
    def authorize(self, _intent: FiscalIntent) -> FiscalResult:
        raise RuntimeError("senha-a1-super-secreta")


def intent() -> dict[str, object]:
    return {
        "documentId": "doc-1", "requestUuid": "req-1", "orderId": "order-1",
        "tenantId": "tenant", "storeId": "store", "deviceId": "device",
        "model": 65, "series": 1, "number": 1, "environment": "homologation",
        "certificateRef": "loja/a1.pfx", "cscRef": "loja/csc", "cscId": "1",
        "issuer": {"uf": "RJ", "cnpj": "12345678000190", "stateRegistration": "123",
                   "taxRegime": 1, "legalName": "Loja Teste", "address": {}},
        "totalCents": 700,
        "items": [{"productId": "p1", "name": "Cafe", "quantity": "1",
                   "unitPriceCents": 700, "totalCents": 700, "ncm": "21011200",
                   "cfop": "5102", "unitCode": "UN", "origin": 0,
                   "csosn": "102", "cstPis": "49", "cstCofins": "49"}],
    }


def client(tmp_path: Path) -> tuple[TestClient, FakeEngine]:
    engine = FakeEngine()
    app = create_app(engine=engine, store=ResultStore(tmp_path / "state.sqlite3"), token="secret")
    return TestClient(app), engine


def test_internal_routes_require_token(tmp_path: Path) -> None:
    http, _ = client(tmp_path)
    assert http.post("/v1/fiscal/authorize", json=intent()).status_code == 401
    assert http.post("/v1/fiscal/authorize", json=intent(),
                     headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_same_request_is_authorized_once_even_after_restart(tmp_path: Path) -> None:
    http, engine = client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    first = http.post("/v1/fiscal/authorize", json=intent(), headers=headers)
    second = http.post("/v1/fiscal/authorize", json=intent(), headers=headers)
    assert first.json()["status"] == "authorized"
    assert second.json() == first.json()
    assert engine.calls == 1


def test_status_returns_durable_result(tmp_path: Path) -> None:
    http, _ = client(tmp_path)
    headers = {"Authorization": "Bearer secret"}
    http.post("/v1/fiscal/authorize", json=intent(), headers=headers)
    status = http.post("/v1/fiscal/status", json={"request_uuid": "req-1"}, headers=headers)
    assert status.json()["code"] == "100"


def test_secret_reference_cannot_escape_mount(tmp_path: Path) -> None:
    resolver = SecretResolver(tmp_path / "secrets")
    try:
        resolver.path("../outside.pfx")
    except SecretError:
        pass
    else:
        raise AssertionError("path traversal aceito")


def test_engine_exception_becomes_unknown_without_leaking_secret(tmp_path: Path) -> None:
    app = create_app(engine=BrokenEngine(), store=ResultStore(tmp_path / "state.sqlite3"),
                     token="secret")
    response = TestClient(app).post("/v1/fiscal/authorize", json=intent(),
                                    headers={"Authorization": "Bearer secret"})
    assert response.json()["status"] == "unknown"
    assert "senha-a1" not in response.text
