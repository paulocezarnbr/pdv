"""Cofre de segredos do terminal — DPAPI do Windows.

Guarda o `device_secret` (chave HMAC do ledger de auditoria) e o token de
sincronização. **Nunca** no banco que eles protegem: se o segredo morasse no
mesmo arquivo SQLite que o operador pode abrir, a proteção seria decorativa.

O que o DPAPI entrega
---------------------

`CryptProtectData` com escopo de máquina cifra o dado com uma chave derivada do
Windows daquela instalação. O arquivo cifrado copiado para outro computador
**não abre**. Isso frustra o ataque mais provável: copiar o `.db` e os arquivos
de config para casa e trabalhar com calma.

O que o DPAPI **não** entrega
-----------------------------

Escopo de máquina significa que qualquer processo naquela máquina pode pedir a
decifragem. Um administrador local, portanto, recupera o segredo — não há
solução local para isso (ver `packaging/README.md`). A garantia forte continua
sendo a ancoragem no servidor: depois do ACK, adulterar a cópia local não muda
o que já está na nuvem.

Escolhemos escopo de **máquina** e não de usuário porque o PDV pode rodar como
serviço ou sob contas diferentes (turno da manhã e da noite), e o segredo
precisa ser o mesmo para a cadeia de auditoria não quebrar na troca de turno.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets as pysecrets
from pathlib import Path

logger = logging.getLogger(__name__)

SECRET_LENGTH_BYTES = 32
_ENTROPY = b"ERPFood.PDV.v1"
"""Entropia adicional do DPAPI: amarra o blob a esta aplicação."""


class SecretVault:
    """Persiste segredos cifrados em disco."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.bin"

    # -- API ------------------------------------------------------------------ #

    def store(self, name: str, value: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        blob = _protect(value)
        path = self._path(name)

        # Escrita atômica: um corte de energia no meio não pode deixar o
        # segredo truncado. Sem a chave, a cadeia de auditoria inteira fica
        # inverificável — e o terminal, inutilizável.
        temp = path.with_suffix(".tmp")
        temp.write_bytes(blob)
        os.replace(temp, path)

    def load(self, name: str) -> bytes | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return _unprotect(path.read_bytes())
        except Exception as exc:  # pragma: no cover
            logger.error("Falha ao decifrar o segredo %r: %s", name, exc)
            return None

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def ensure_device_secret(self) -> bytes:
        """Devolve o segredo do terminal, criando-o na primeira execução.

        Gerar localmente (e não receber do servidor) evita que a chave trafegue
        pela rede. O servidor recebe apenas a chave pública do pareamento
        durante a ativação.
        """
        existing = self.load("device_secret")
        if existing is not None:
            return existing

        generated = pysecrets.token_bytes(SECRET_LENGTH_BYTES)
        self.store("device_secret", generated)
        logger.info("device_secret gerado e protegido no DPAPI")
        return generated


# --------------------------------------------------------------------------- #
# DPAPI
# --------------------------------------------------------------------------- #


def _protect(data: bytes) -> bytes:
    """Cifra com DPAPI (escopo de máquina).

    Fora do Windows cai para Base64 — **sem proteção real**, apenas para o
    desenvolvimento em Linux/macOS não quebrar. O aviso é gritado no log
    justamente para ninguém confundir isso com segurança.
    """
    try:
        import win32crypt
    except ImportError:
        logger.warning(
            "DPAPI indisponível (fora do Windows): segredo NÃO está protegido. "
            "Aceitável só em desenvolvimento."
        )
        return b"PLAIN:" + base64.b64encode(data)

    return win32crypt.CryptProtectData(
        data,
        "PDV device secret",
        _ENTROPY,
        None,
        None,
        0x4,  # CRYPTPROTECT_LOCAL_MACHINE
    )


def _unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"PLAIN:"):
        return base64.b64decode(blob[6:])

    import win32crypt

    _description, data = win32crypt.CryptUnprotectData(
        blob, _ENTROPY, None, None, 0x4
    )
    return data
