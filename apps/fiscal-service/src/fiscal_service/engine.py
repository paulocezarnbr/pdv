from __future__ import annotations

from typing import Protocol

from fiscal_service.models import FiscalIntent, FiscalResult


class FiscalEngine(Protocol):
    def authorize(self, intent: FiscalIntent) -> FiscalResult: ...


class HomologationGateEngine:
    """Trava explícita até o motor QR Code v3 passar na homologação do RJ.

    O PyNFe é usado para transporte/certificado no adaptador do projeto, mas a
    geração QR Code v3 ainda não pode ser declarada pronta. Rejeitar é mais
    seguro que inventar XML fiscal ou aceitar produção por acidente.
    """

    def authorize(self, intent: FiscalIntent) -> FiscalResult:
        return FiscalResult(
            status="rejected",
            code="FISCAL_ENGINE_NOT_HOMOLOGATED",
            reason=(
                "Motor NFC-e QR Code v3 ainda não homologado para RJ. "
                "A reserva foi preservada; não emita outro número."
            ),
        )
