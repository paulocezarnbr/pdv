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


# --------------------------------------------------------------------------- #
# Comandos do painel (Fase 3.5.b)
# --------------------------------------------------------------------------- #
#
# Protocolo **separado** do `Transport`, e não métodos novos nele, por dois
# motivos práticos:
#
# * uma nuvem antiga que ainda não fala de comando continua servindo o terminal
#   novo — o ciclo de comando simplesmente não roda, e a venda continua subindo,
#   que é o que não pode parar;
# * o `Transport` falso dos testes de sincronização não precisa aprender a
#   responder comando para continuar testando push e pull.


@dataclass(frozen=True, slots=True)
class CommandFetch:
    """Pedido de comandos endereçados a **este** terminal."""

    tenant_id: str
    store_id: str
    device_id: str
    limit: int = 50


@dataclass(frozen=True, slots=True)
class CommandDelivery:
    """O que a nuvem entregou.

    A entrega **não** consome o comando na nuvem: ele continua sendo entregue
    até o terminal relatar o que fez com ele. Consumir na entrega perderia o
    comando de vez se o terminal morresse entre receber e gravar na inbox — e
    perder um comando em silêncio é pior que entregá-lo duas vezes, porque a
    segunda entrega colide no `command_uuid` e vira no-op.
    """

    commands: tuple[Any, ...] = ()
    """Cada item é um `pdv.remote.protocol.RemoteCommand`, **não verificado**.

    O transporte carrega bytes; quem confere assinatura, validade e teto é o
    `RemoteCommandService`. Verificar aqui espalharia a decisão de segurança
    por duas camadas, e a camada que fala com a rede é a errada para tê-la.
    """


@dataclass(frozen=True, slots=True)
class CommandReport:
    """O que o terminal fez com os comandos que recebeu."""

    tenant_id: str
    store_id: str
    device_id: str
    results: tuple[Any, ...] = ()
    """Cada item é um `pdv.remote.inbox.CommandResult`."""
    awaiting: tuple[Any, ...] = ()
    """Cada item é um `pdv.remote.inbox.AwaitingNotice`.

    Viaja num campo à parte, e não como um terceiro status em `results`: uma
    nuvem que ainda não conhece a espera ignora o campo e continua aceitando os
    resultados. Um status novo dentro de `results` faria ela recusar o lote
    inteiro — e o desconto aplicado ficaria "pendente" no painel por causa de
    um aviso que nem era dele.
    """


class CommandTransport(Protocol):
    """Canal de comandos. Opcional: nem toda nuvem o implementa."""

    def fetch_commands(self, request: CommandFetch) -> CommandDelivery:
        """Busca os comandos pendentes para este terminal."""
        ...

    def report_commands(self, report: CommandReport) -> tuple[str, ...]:
        """Relata os resultados. Devolve os `command_uuid` que a nuvem aceitou.

        Só o que a nuvem confirmar sai da fila de relato. Assumir que ela
        recebeu faria o painel ficar mostrando `pendente` para sempre num
        comando que o terminal já aplicou — e é exatamente nesse estado que
        alguém reemite o desconto na mão.
        """
        ...


def speaks_commands(transport: object) -> bool:
    """O transporte sabe falar de comando?

    Checagem estrutural, e não `isinstance`: `Protocol` sem `runtime_checkable`
    não suporta `isinstance`, e marcá-lo assim só verificaria a existência dos
    nomes — exatamente o que estas duas linhas fazem, sem a cerimônia.
    """
    return callable(getattr(transport, "fetch_commands", None)) and callable(
        getattr(transport, "report_commands", None)
    )


@dataclass(frozen=True, slots=True)
class CommandCycleReport:
    """Resultado de um ciclo de comandos."""

    fetched: int = 0
    accepted: int = 0
    applied: int = 0
    refused: int = 0
    reported: int = 0
    awaiting: int = 0
    error: str | None = None


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
