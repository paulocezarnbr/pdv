"""Contratos da sincronização — DTOs e a interface de transporte.

O transporte é uma `Protocol` e não uma classe concreta de HTTP porque a lógica
de sincronização precisa ser testável contra **falhas de rede específicas**:
queda antes do commit do servidor, queda depois do commit mas antes da resposta
(o caso perigoso), resposta parcial, servidor rejeitando item. Com a interface
isolada, um servidor falso no teste reproduz cada cenário de forma determinística
— algo impossível de fazer de modo confiável contra rede real.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class SyncError(Exception):
    """Raiz das falhas de sincronização."""


class TransportError(SyncError):
    """Falha de rede/servidor. É sempre **retentável** — o lote volta à fila.

    Distinguir isto de `ItemStatus.REJECTED` é essencial: erro de transporte
    significa "não sei se chegou", e a resposta correta é reenviar. Rejeição
    significa "chegou e o servidor recusou", e reenviar não adianta.
    """


class AuthError(SyncError):
    """Credencial do dispositivo inválida ou revogada. Não retentável."""


class ItemStatus(Enum):
    """Veredito do servidor para cada item do lote."""

    APPLIED = "applied"
    """Gravado agora."""

    DUPLICATE = "duplicate"
    """Já existia — reenvio após resposta perdida. **Também conta como sucesso.**"""

    REJECTED = "rejected"
    """Recusado (payload inválido, cadeia de auditoria quebrada, seq regredido)."""

    @property
    def is_settled(self) -> bool:
        """`True` quando o dado está seguro no servidor.

        `DUPLICATE` é sucesso: significa que uma tentativa anterior chegou e só
        a resposta se perdeu. Tratar duplicata como erro faria o cliente
        reenviar para sempre.
        """
        return self in (ItemStatus.APPLIED, ItemStatus.DUPLICATE)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """Uma linha da fila de saída, pronta para subir."""

    seq: int
    entity_table: str
    entity_id: str
    client_uuid: str
    operation: str
    payload: dict[str, Any]
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class PushBatch:
    """Lote enviado ao servidor."""

    device_id: str
    tenant_id: str
    store_id: str
    items: tuple[OutboxItem, ...]

    @property
    def idempotency_key(self) -> str:
        """Chave estável para o **mesmo conteúdo** de lote.

        Derivada do conteúdo, não do relógio nem de aleatoriedade: uma retentativa
        após timeout produz exatamente a mesma chave, e o servidor reconhece a
        repetição mesmo que o primeiro envio tenha sido aplicado e a resposta
        perdida no caminho.
        """
        material = "|".join(item.client_uuid for item in self.items)
        digest = hashlib.sha256(f"{self.device_id}|{material}".encode()).hexdigest()
        return digest[:32]


@dataclass(frozen=True, slots=True)
class ItemAck:
    """Confirmação por item. O servidor responde um destes para cada envio."""

    client_uuid: str
    status: ItemStatus
    message: str | None = None
    server_seq: int | None = None


@dataclass(frozen=True, slots=True)
class PushResponse:
    acks: tuple[ItemAck, ...]

    def by_uuid(self) -> dict[str, ItemAck]:
        return {ack.client_uuid: ack for ack in self.acks}


@dataclass(frozen=True, slots=True)
class PullRequest:
    tenant_id: str
    store_id: str
    entity_table: str
    since_server_seq: int
    limit: int = 500


@dataclass(frozen=True, slots=True)
class PullResponse:
    entity_table: str
    rows: tuple[dict[str, Any], ...]
    last_server_seq: int
    has_more: bool = False


class Transport(Protocol):
    """Canal até a nuvem. Implementado por HTTP em produção, falso em teste."""

    def push(self, batch: PushBatch) -> PushResponse:
        """Envia o lote.

        Raises:
            TransportError: falha retentável (rede, 5xx, timeout).
            AuthError: credencial inválida — não adianta reenviar.
        """
        ...

    def pull(self, request: PullRequest) -> PullResponse:
        """Baixa alterações de cadastro feitas na retaguarda."""
        ...


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Resultado de um ciclo de sincronização."""

    sent: int = 0
    settled: int = 0
    rejected: int = 0
    deferred: int = 0
    error: str | None = None

    @property
    def made_progress(self) -> bool:
        return self.settled > 0
