"""Resultado e gorjeta por funcionário.

Para que serve
--------------

Duas perguntas que a loja faz todo fim de turno e que o sistema não respondia:

* **Quanto cada garçom atendeu?** Mesas, itens e valor. É o que sustenta escala
  e comissão, e é a conversa que hoje acontece no olho.
* **Quanto de gorjeta é de quem?** A gorjeta chega na mão do caixa junto com a
  conta. Sem registro de quem atendeu aquela mesa, a divisão no fim da noite é
  memória contra memória — e é onde a confiança da equipe se perde.

Nada disso era possível enquanto o pedido era atribuído ao **celular**. Ver o
cabeçalho de `edge/staff.py`.

O dia de food service não começa à meia-noite
---------------------------------------------

Uma mesa que senta às 23h40 e paga às 00h20 pertence ao turno da noite, não ao
dia seguinte. Cortar em UTC-0 seria pior ainda: no Brasil partiria o jantar ao
meio, às 21h. Por isso o corte é às 5h da manhã do **fuso da máquina** — depois
que a última cozinha do país fechou e antes de qualquer loja abrir.

O que este módulo não é
-----------------------

Não é fechamento de caixa e não substitui a conciliação. Ele soma o que está no
banco local deste terminal; a verdade fiscal do mês é a nuvem, que recebe os
mesmos pedidos pelo outbox. Uma loja com dois terminais precisa somar os dois —
por isso os números aparecem no painel como *resultado do turno*, e não como
*faturamento*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.domain.models import Cents, EntityId, iso

logger = logging.getLogger(__name__)

#: Hora local em que um dia de operação vira o outro. Ver o cabeçalho.
BUSINESS_DAY_START = time(5, 0)


@dataclass(frozen=True, slots=True)
class WaiterResult:
    """O que uma pessoa fez no período."""

    user_id: EntityId
    name: str
    role: str
    orders: int
    items: int
    total_cents: Cents
    tip_cents: Cents
    open_orders: int

    @property
    def average_ticket_cents(self) -> Cents:
        """Ticket médio. Zero comandas dá zero, não divisão por zero."""
        return Cents(int(self.total_cents) // self.orders) if self.orders else Cents(0)

    def to_json(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "orders": self.orders,
            "items": self.items,
            "total_cents": int(self.total_cents),
            "tip_cents": int(self.tip_cents),
            "open_orders": self.open_orders,
            "average_ticket_cents": int(self.average_ticket_cents),
        }


def business_day_window(now: datetime | None = None) -> tuple[str, str]:
    """Início e fim (ISO, UTC) do dia de operação que contém `now`.

    Devolve o par pronto para comparar com as colunas do banco, que guardam
    ISO-8601 em UTC — comparação lexicográfica funciona porque o formato é
    fixo e o fuso é sempre o mesmo.
    """
    from pdv.domain.models import utc_now

    moment = (now or utc_now()).astimezone()
    start_local = datetime.combine(
        moment.date(), BUSINESS_DAY_START, tzinfo=moment.tzinfo
    )
    if moment < start_local:
        # Ainda é madrugada: pertence ao dia anterior.
        start_local -= timedelta(days=1)
    return iso(start_local), iso(start_local + timedelta(days=1))


class StaffReport:
    """Lê o resultado por pessoa a partir dos pedidos deste terminal."""

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config

    # -- consultas ------------------------------------------------------------ #

    def by_waiter(self, window: tuple[str, str] | None = None) -> list[WaiterResult]:
        """O turno inteiro, uma linha por pessoa que atendeu.

        Só quem **atendeu** aparece. Listar toda a folha com zeros transformaria
        o relatório num ranking de quem não trabalhou hoje, que é o oposto do
        que ele serve para responder — e quem está de folga apareceria como
        quem não vendeu.
        """
        start, end = window or business_day_window()
        rows = self._db.query_all(
            """
            SELECT o.operator_id                              AS user_id,
                   COALESCE(u.name, 'Sem cadastro')           AS name,
                   COALESCE(u.role, '—')                      AS role,
                   SUM(o.status = 'paid')                     AS orders,
                   SUM(CASE WHEN o.status = 'paid'
                            THEN o.total_cents ELSE 0 END)    AS total_cents,
                   SUM(CASE WHEN o.status = 'paid'
                            THEN o.tip_cents ELSE 0 END)      AS tip_cents,
                   SUM(o.status = 'open')                     AS open_orders,
                   (SELECT COUNT(*) FROM order_items i
                     JOIN orders oo ON oo.id = i.order_id
                    WHERE i.canceled_at IS NULL
                      AND oo.tenant_id = o.tenant_id
                      AND oo.channel = 'waiter'
                      AND oo.opened_at >= ? AND oo.opened_at < ?
                      -- Item atribuído a quem o lançou quando o app informou,
                      -- e a quem abriu a comanda quando não informou (aparelho
                      -- antigo, ou pedido anterior à sessão de garçom).
                      AND COALESCE(i.created_by_user_id, oo.operator_id)
                          = o.operator_id)                    AS items
              FROM orders o
              LEFT JOIN users u ON u.id = o.operator_id
             WHERE o.tenant_id = ?
               AND o.channel = 'waiter'
               AND o.status IN ('open', 'paid')
               AND o.opened_at >= ? AND o.opened_at < ?
             GROUP BY o.operator_id
             ORDER BY total_cents DESC, name
            """,
            (start, end, self._config.tenant_id, start, end),
        )
        return [_to_result(row) for row in rows]

    def for_user(
        self, user_id: EntityId, window: tuple[str, str] | None = None
    ) -> dict[str, object]:
        """O resultado de uma pessoa só, para ela ver no próprio app."""
        found = next(
            (r for r in self.by_waiter(window) if r.user_id == user_id), None
        )
        if found is not None:
            return found.to_json()

        # Quem entrou e ainda não atendeu ninguém não é erro: é o começo do
        # turno. Devolver 404 faria o app mostrar falha na primeira abertura.
        identity = self._identity(user_id)
        return WaiterResult(
            user_id=user_id,
            name=identity[0],
            role=identity[1],
            orders=0,
            items=0,
            total_cents=Cents(0),
            tip_cents=Cents(0),
            open_orders=0,
        ).to_json()

    def totals(self, window: tuple[str, str] | None = None) -> dict[str, int]:
        """A soma do salão no período — o rodapé do relatório."""
        results = self.by_waiter(window)
        return {
            "orders": sum(r.orders for r in results),
            "items": sum(r.items for r in results),
            "total_cents": sum(int(r.total_cents) for r in results),
            "tip_cents": sum(int(r.tip_cents) for r in results),
            "open_orders": sum(r.open_orders for r in results),
        }

    def _identity(self, user_id: EntityId) -> tuple[str, str]:
        row = self._db.query_one(
            "SELECT name, role FROM users WHERE id = ? AND tenant_id = ?",
            (user_id, self._config.tenant_id),
        )
        return (
            (str(row["name"]), str(row["role"]))
            if row is not None
            else ("Sem cadastro", "—")
        )


def _to_result(row) -> WaiterResult:  # noqa: ANN001
    return WaiterResult(
        user_id=EntityId(str(row["user_id"] or "")),
        name=str(row["name"]),
        role=str(row["role"]),
        orders=int(row["orders"] or 0),
        items=int(row["items"] or 0),
        total_cents=Cents(int(row["total_cents"] or 0)),
        tip_cents=Cents(int(row["tip_cents"] or 0)),
        open_orders=int(row["open_orders"] or 0),
    )


__all__ = [
    "BUSINESS_DAY_START",
    "StaffReport",
    "WaiterResult",
    "business_day_window",
]
