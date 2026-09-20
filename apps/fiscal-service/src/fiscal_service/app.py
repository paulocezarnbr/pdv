from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

from fiscal_service.engine import FiscalEngine, HomologationGateEngine
from fiscal_service.models import FiscalIntent, FiscalResult, StatusRequest
from fiscal_service.security import authenticate
from fiscal_service.store import ResultStore


def create_app(
    *, engine: FiscalEngine | None = None, store: ResultStore | None = None,
    token: str | None = None,
) -> FastAPI:
    expected = token if token is not None else os.getenv("FISCAL_SERVICE_TOKEN", "")
    state = store or ResultStore(Path(os.getenv("FISCAL_STATE_DB", "data/fiscal-state.sqlite3")))
    fiscal_engine = engine or HomologationGateEngine()
    application = FastAPI(title="ERP Food Fiscal", docs_url=None, redoc_url=None)

    def authorized(authorization: str | None = Header(default=None)) -> None:
        if not expected or not authenticate(authorization, expected):
            raise HTTPException(status_code=401, detail="Serviço interno não autenticado.")

    @application.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "engine": "homologation-gated"}

    @application.post("/v1/fiscal/authorize", response_model=FiscalResult)
    def authorize(intent: FiscalIntent, _auth: None = Depends(authorized)) -> FiscalResult:
        prior = state.get(intent.requestUuid)
        if prior is not None:
            return prior
        if not state.claim(intent.requestUuid, intent.documentId):
            # Outra chamada está em voo. Não a repete: o estado é ambíguo.
            return FiscalResult(status="unknown", code="PROCESSING",
                                reason="Solicitação fiscal ainda está em processamento.")
        try:
            result = fiscal_engine.authorize(intent)
        except Exception:
            # A exceção real fica fora da resposta: bibliotecas fiscais podem
            # incluir caminho do A1 ou detalhe criptográfico na mensagem.
            result = FiscalResult(
                status="unknown", code="ENGINE_FAILURE",
                reason="Falha interna após o início da autorização; consulte antes de reenviar.",
            )
        state.settle(intent.requestUuid, result)
        return result

    @application.post("/v1/fiscal/status", response_model=FiscalResult)
    def status(query: StatusRequest, _auth: None = Depends(authorized)) -> FiscalResult:
        result = state.get(query.request_uuid)
        if result is None:
            return FiscalResult(status="unknown", code="NOT_SETTLED",
                                reason="Solicitação não concluída neste serviço.")
        return result

    return application


app = create_app()
