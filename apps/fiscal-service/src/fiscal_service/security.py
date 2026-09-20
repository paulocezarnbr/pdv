from __future__ import annotations

import hmac
import os
from pathlib import Path


class SecretError(RuntimeError):
    pass


def authenticate(offered: str | None, expected: str) -> bool:
    if not offered or not offered.startswith("Bearer "):
        return False
    token = offered.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, expected)


class SecretResolver:
    """Resolve referências dentro de uma pasta montada, sem aceitar caminhos."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or Path(os.getenv("FISCAL_SECRETS_DIR", "/run/secrets/fiscal"))).resolve()

    def path(self, reference: str) -> Path:
        if not reference or Path(reference).is_absolute():
            raise SecretError("Referência de segredo inválida.")
        candidate = (self._root / reference).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise SecretError("Referência de segredo fora da área permitida.") from exc
        if not candidate.is_file():
            raise SecretError("Segredo fiscal não provisionado.")
        return candidate

    def text(self, reference: str) -> str:
        value = self.path(reference).read_text(encoding="utf-8").strip()
        if not value:
            raise SecretError("Segredo fiscal vazio.")
        return value
