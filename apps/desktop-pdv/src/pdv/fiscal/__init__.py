"""Núcleo fiscal offline-first; adaptadores SEFAZ ficam fora do domínio."""

from pdv.fiscal.service import FiscalDocument, FiscalError, FiscalService

__all__ = ["FiscalDocument", "FiscalError", "FiscalService"]
