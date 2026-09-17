"""Fila da cozinha — o KDS.

O ciclo de vida do ticket é o da **cozinha**, não o da venda: um item pago pode
ainda não ter saído, e um item pronto pode voltar. Por isso `kds_tickets` é uma
tabela própria e não um campo em `order_items`.

    queued ──▶ preparing ──▶ ready ──▶ delivered
       ▲            │           │
       └──── recall ─┴───────────┘

**Bump** avança; **recall** volta. Recall existe porque a cozinha erra: bateu
pronto no prato errado e precisa desfazer sem que a única saída seja cancelar o
item — cancelar mexeria na venda, o que é decisão de gerente, não de quem está
na chapa.

Tempo é medido do `queued_at`, não do momento em que a cozinha começou. O que
interessa ao cliente sentado à mesa é há quanto tempo ele pediu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.domain.models import EntityId, iso, utc_now
from pdv.edge.hub import Event, EventHub

logger = logging.getLogger(__name__)

#: Minutos no `queued`/`preparing` antes de a tela marcar o ticket como atrasado.
LATE_THRESHOLD_MINUTES = 15

#: Transições permitidas. Tudo que não está aqui é recusado — uma tela com
#: versão antiga não pode inventar estado novo no banco da loja.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"preparing", "canceled"}),
    "preparing": frozenset({"ready", "queued", "canceled"}),
    "ready": frozenset({"delivered", "preparing"}),
    "delivered": frozenset({"ready"}),
    "canceled": frozenset(),
}

#: Ordem do ciclo e o carimbo que cada etapa grava. A posição na lista é o que
#: define, num recall, quais carimbos ficaram para trás e precisam sair.
_STAGES: tuple[tuple[str, str | None], ...] = (
    ("queued", None),
    ("preparing", "started_at"),
    ("ready", "ready_at"),
    ("delivered", "delivered_at"),
)
_STAGE_INDEX = {name: i for i, (name, _) in enumerate(_STAGES)}


class TicketNotFoundError(PdvError):
    """Ticket inexistente ou de outro tenant."""


class InvalidTransitionError(PdvError):
    """Mudança de estado que a cozinha não pode fazer."""


@dataclass(frozen=True, slots=True)
class Ticket:
    id: EntityId
    order_id: EntityId
    local_number: int
    table_label: str
    station: str
    product_name: str
    quantity: str
    notes: str
    status: str
    queued_at: str
    waiting_seconds: int

    @property
    def is_late(self) -> bool:
        return (
            self.status in ("queued", "preparing")
            and self.waiting_seconds >= LATE_THRESHOLD_MINUTES * 60
        )

    def to_json(self) -> dict[str, object]:
        return {
            "ticket_id": self.id,
            "order_id": self.order_id,
            "local_number": self.local_number,
            "table_label": self.table_label,
            "station": self.station,
            "product_name": self.product_name,
            "quantity": self.quantity,
            "notes": self.notes,
            "status": self.status,
            "queued_at": self.queued_at,
            "waiting_seconds": self.waiting_seconds,
            "is_late": self.is_late,
        }


class KdsService:
    """Consulta e avanço da fila da cozinha."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        hub: EventHub | None = None,
    ) -> None:
        self._db = database
        self._config = config
        self._hub = hub or EventHub()

    def list_active(self, station: str | None = None) -> list[Ticket]:
        """Tickets que ainda importam para a cozinha.

        `delivered` e `canceled` ficam de fora: a tela precisa do que falta
        fazer. O histórico do dia é relatório, não fila.
        """
        sql = (
            "SELECT t.*, o.local_number, o.customer_id "
            "  FROM kds_tickets t JOIN orders o ON o.id = t.order_id "
            " WHERE t.tenant_id = ? AND t.status IN ('queued','preparing','ready') "
        )
        params: list[object] = [self._config.tenant_id]
        if station:
            sql += "   AND t.station = ? "
            params.append(station)
        sql += " ORDER BY t.queued_at"

        now = utc_now()
        return [_to_ticket(row, now) for row in self._db.query_all(sql, tuple(params))]

    def advance(self, ticket_id: EntityId, to_status: str) -> Ticket:
        """Move o ticket, validando a transição.

        Raises:
            TicketNotFoundError: ticket inexistente.
            InvalidTransitionError: transição não permitida.
        """
        current = self._require(ticket_id)

        if to_status not in _TRANSITIONS.get(current.status, frozenset()):
            raise InvalidTransitionError(
                f"Não é possível ir de {current.status!r} para {to_status!r}."
            )

        now = iso(utc_now())
        target = _STAGE_INDEX[to_status] if to_status in _STAGE_INDEX else None

        assignments = ["status = ?", "updated_at = ?"]
        params: list[object] = [to_status, now]

        if target is not None:
            # Carimba a etapa alcançada...
            own_column = _STAGES[target][1]
            if own_column:
                assignments.append(f"{own_column} = ?")
                params.append(now)

            # ...e apaga o carimbo de toda etapa que ficou à frente.
            #
            # Num recall de `ready` para `preparing`, manter `ready_at` faria o
            # relatório de tempo de preparo contar como pronto um prato que
            # voltou para a chapa — a cozinha pareceria mais rápida do que é,
            # justamente nos casos em que errou.
            for name, column in _STAGES[target + 1 :]:
                if column:
                    assignments.append(f"{column} = NULL")

        params.append(ticket_id)
        with self._db.transaction() as connection:
            connection.execute(
                f"UPDATE kds_tickets SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )

        updated = self._require(ticket_id)
        self._hub.publish(Event("ticket.changed", updated.to_json()))
        logger.info(
            "Ticket %s: %s → %s", ticket_id[:8], current.status, to_status
        )
        return updated

    def bump(self, ticket_id: EntityId) -> Ticket:
        """Avanço natural: fila → preparo → pronto → entregue."""
        current = self._require(ticket_id)
        nxt = {"queued": "preparing", "preparing": "ready", "ready": "delivered"}
        if current.status not in nxt:
            raise InvalidTransitionError(
                f"Ticket já está {current.status!r}; não há próximo passo."
            )
        return self.advance(ticket_id, nxt[current.status])

    def recall(self, ticket_id: EntityId) -> Ticket:
        """Desfaz o último avanço — a cozinha bateu pronto no prato errado."""
        current = self._require(ticket_id)
        previous = {"preparing": "queued", "ready": "preparing", "delivered": "ready"}
        if current.status not in previous:
            raise InvalidTransitionError(
                f"Ticket está {current.status!r}; não há o que desfazer."
            )
        return self.advance(ticket_id, previous[current.status])

    def _require(self, ticket_id: EntityId) -> Ticket:
        row = self._db.query_one(
            "SELECT t.*, o.local_number, o.customer_id "
            "  FROM kds_tickets t JOIN orders o ON o.id = t.order_id "
            " WHERE t.id = ? AND t.tenant_id = ?",
            (ticket_id, self._config.tenant_id),
        )
        if row is None:
            raise TicketNotFoundError(f"Ticket {ticket_id} não encontrado.")
        return _to_ticket(row, utc_now())


def _to_ticket(row, now: datetime) -> Ticket:  # noqa: ANN001
    queued_at = str(row["queued_at"])
    try:
        queued = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
        waiting = max(0, int((now - queued).total_seconds()))
    except ValueError:  # pragma: no cover - carimbo corrompido não derruba a tela
        waiting = 0

    return Ticket(
        id=EntityId(str(row["id"])),
        order_id=EntityId(str(row["order_id"])),
        local_number=int(row["local_number"]),
        table_label=str(row["customer_id"] or ""),
        station=str(row["station"]),
        product_name=str(row["product_name"]),
        quantity=str(row["quantity"]),
        notes=str(row["notes"] or ""),
        status=str(row["status"]),
        queued_at=queued_at,
        waiting_seconds=waiting,
    )
