"""Núcleo fiscal offline-first; adaptadores SEFAZ ficam fora do domínio."""

from pdv.fiscal.gateway import (
    AuthorizationResult,
    FiscalCommunicationError,
    FiscalGateway,
    PyNFeGateway,
    SefazStatus,
)
from pdv.fiscal.service import FiscalDocument, FiscalError, FiscalService

__all__ = [
    "AuthorizationResult", "FiscalCommunicationError", "FiscalDocument",
    "FiscalError", "FiscalGateway", "FiscalService", "PyNFeGateway", "SefazStatus",
]
