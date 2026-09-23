"""Núcleo fiscal offline-first; adaptadores SEFAZ ficam fora do domínio."""

from pdv.fiscal.cloud import FiscalCoordinator, HttpCloudFiscalGateway
from pdv.fiscal.gateway import (
    AuthorizationResult,
    FiscalCommunicationError,
    FiscalGateway,
    PyNFeGateway,
    SefazStatus,
)
from pdv.fiscal.service import (
    FiscalDocument,
    FiscalError,
    FiscalNotRequired,
    FiscalService,
)

# Uma lista só. A versão anterior redefinia `__all__` no fim do arquivo, e a
# segunda atribuição apagava a primeira: `from pdv.fiscal import *` deixava de
# exportar `FiscalService` sem erro nenhum.
__all__ = [
    "AuthorizationResult",
    "FiscalCommunicationError",
    "FiscalCoordinator",
    "FiscalDocument",
    "FiscalError",
    "FiscalGateway",
    "FiscalNotRequired",
    "FiscalService",
    "HttpCloudFiscalGateway",
    "PyNFeGateway",
    "SefazStatus",
]
