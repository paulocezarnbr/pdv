"""O mapa de mesas do salão.

Por que a mesa deixou de ser texto
----------------------------------

Na Fase 3 a mesa era um `table_label` digitado no celular e guardado em
`orders.customer_id`. Isso provou o fluxo e falha no salão de verdade por três
caminhos, todos silenciosos:

* **Conta partida.** Dois garçons abriam a "Mesa 5" duas vezes e a mesa ficava
  com duas comandas. Ninguém percebe até a hora de cobrar, e aí não há como
  saber qual das duas o cliente reconhece.
* **Mesas fantasma.** "mesa 5", "Mesa 5" e "M5" eram três mesas diferentes, e o
  relatório de faturamento por mesa virava ruído.
* **Nada a configurar.** O dono não conseguia dizer quantas mesas tem, nem
  separar salão de varanda.

O rótulo continua copiado dentro do pedido. **De propósito**: renomear a mesa
amanhã não pode reescrever o que saiu impresso ontem. A mesa é a entidade viva;
o pedido guarda a fotografia dela.

Desativar, nunca apagar
-----------------------

Mesa que sai do mapa recebe `is_active = 0`. Apagar a linha arrebentaria as
comandas antigas que apontam para ela — e o relatório de faturamento por mesa
perderia todo o passado para economizar uma linha de banco.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import Cents, EntityId, iso, new_id, utc_now

logger = logging.getLogger(__name__)

#: Limite de mesas por loja. Não é regra de negócio — é o teto que impede um
#: laço defeituoso no app de criar dez mil mesas antes de alguém perceber.
MAX_TABLES = 300


class TableError(PdvError):
    """Problema com a mesa: rótulo repetido, mesa inexistente ou ocupada."""


@dataclass(frozen=True, slots=True)
class StoreTable:
    id: EntityId
    label: str
    area: str
    seats: int
    sort_order: int
    is_active: bool

    #: Ocupação — `None` quando a mesa está livre.
    order_id: EntityId | None = None
    local_number: int | None = None
    total_cents: Cents = Cents(0)
    item_count: int = 0
    opened_at: str | None = None
    bill_requested_at: str | None = None

    @property
    def occupied(self) -> bool:
        return self.order_id is not None

    @property
    def status(self) -> str:
        """O estado que o app pinta na mesa."""
        if not self.occupied:
            return "free"
        return "billing" if self.bill_requested_at else "busy"

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "area": self.area,
            "seats": self.seats,
            "sort_order": self.sort_order,
            "is_active": self.is_active,
            "status": self.status,
            "order_id": self.order_id,
            "local_number": self.local_number,
            "total_cents": int(self.total_cents),
            "item_count": self.item_count,
            "opened_at": self.opened_at,
            "bill_requested_at": self.bill_requested_at,
        }


class TableService:
    """Cadastro e ocupação das mesas."""

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config
        self._outbox = OutboxRepository()

    # -- leitura -------------------------------------------------------------- #

    def list_tables(self, *, include_inactive: bool = False) -> list[StoreTable]:
        """O mapa do salão, com a ocupação de cada mesa numa consulta só.

        Uma consulta por mesa faria o app do garçom disparar trinta chamadas a
        cada atualização, no Wi-Fi da loja, com o celular no bolso.
        """
        rows = self._db.query_all(
            """
            SELECT t.*,
                   o.id            AS order_id,
                   o.local_number  AS local_number,
                   o.total_cents   AS order_total,
                   o.opened_at     AS opened_at,
                   o.bill_requested_at AS bill_requested_at,
                   (SELECT COUNT(*) FROM order_items i
                     WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items
              FROM store_tables t
              LEFT JOIN orders o
                     ON o.table_id = t.id
                    AND o.status = 'open'
                    AND o.tenant_id = t.tenant_id
             WHERE t.tenant_id = ? AND t.store_id = ?
               AND (t.is_active = 1 OR ?)
             ORDER BY t.sort_order, t.label
            """,
            (self._config.tenant_id, self._config.store_id, int(include_inactive)),
        )
        return [_to_table(row) for row in rows]

    def get(self, table_id: EntityId) -> StoreTable:
        rows = [t for t in self.list_tables(include_inactive=True) if t.id == table_id]
        if not rows:
            raise TableError("Mesa não encontrada.")
        return rows[0]

    def find_by_label(self, label: str) -> StoreTable | None:
        wanted = label.strip().lower()
        return next(
            (t for t in self.list_tables() if t.label.lower() == wanted), None
        )

    # -- escrita -------------------------------------------------------------- #

    def create(
        self,
        *,
        label: str,
        area: str = "Salão",
        seats: int = 4,
        sort_order: int | None = None,
    ) -> StoreTable:
        label = _clean(label, "O rótulo da mesa não pode ficar em branco.", 32)
        area = _clean(area, "A área não pode ficar em branco.", 32)
        seats = max(1, min(int(seats), 99))

        active = [t for t in self.list_tables()]
        if len(active) >= MAX_TABLES:
            raise TableError(f"Limite de {MAX_TABLES} mesas por loja atingido.")
        if sort_order is None:
            sort_order = max((t.sort_order for t in active), default=0) + 1

        table_id = new_id()
        client_uuid = new_id()
        now = iso(utc_now())
        try:
            with self._db.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO store_tables
                        (id, tenant_id, store_id, label, area, seats, sort_order,
                         is_active, created_at, updated_at, client_uuid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        table_id, self._config.tenant_id, self._config.store_id,
                        label, area, seats, sort_order, now, now, client_uuid,
                    ),
                )
                self._enqueue(connection, table_id, client_uuid, "insert", {
                    "label": label, "area": area, "seats": seats,
                    "sort_order": sort_order, "is_active": True,
                })
        except sqlite3.IntegrityError as exc:
            raise TableError(f"Já existe uma mesa chamada {label!r}.") from exc

        logger.info("Mesa criada: %s", label)
        return self.get(EntityId(table_id))

    def update(
        self,
        table_id: EntityId,
        *,
        label: str | None = None,
        area: str | None = None,
        seats: int | None = None,
        sort_order: int | None = None,
    ) -> StoreTable:
        """Edita o cadastro. Não mexe na comanda aberta.

        Renomear uma mesa ocupada é permitido — o pedido guarda a própria cópia
        do rótulo, então a comanda em curso e o cupom já impresso continuam
        dizendo o que diziam.
        """
        current = self.get(table_id)
        if not current.is_active:
            raise TableError("Mesa desativada. Reative antes de editar.")

        fields: dict[str, object] = {}
        if label is not None:
            fields["label"] = _clean(label, "O rótulo não pode ficar em branco.", 32)
        if area is not None:
            fields["area"] = _clean(area, "A área não pode ficar em branco.", 32)
        if seats is not None:
            fields["seats"] = max(1, min(int(seats), 99))
        if sort_order is not None:
            fields["sort_order"] = int(sort_order)
        if not fields:
            return current

        assignments = ", ".join(f"{name} = ?" for name in fields)
        try:
            with self._db.transaction() as connection:
                connection.execute(
                    f"UPDATE store_tables SET {assignments}, updated_at = ?, "
                    "is_synced = 0 WHERE id = ? AND tenant_id = ?",
                    (*fields.values(), iso(utc_now()), table_id,
                     self._config.tenant_id),
                )
                self._enqueue(connection, str(table_id), new_id(), "update", fields)
        except sqlite3.IntegrityError as exc:
            raise TableError(f"Já existe uma mesa chamada {label!r}.") from exc

        return self.get(table_id)

    def set_active(self, table_id: EntityId, active: bool) -> StoreTable:
        """Tira a mesa do mapa, ou devolve.

        **Mesa ocupada não sai.** Sumir com a mesa deixaria a comanda aberta sem
        nenhum lugar na tela por onde chegar até ela — o pedido continuaria
        existindo, invisível, até alguém achar na conciliação do dia seguinte.
        """
        table = self.get(table_id)
        if not active and table.occupied:
            raise TableError(
                f"A {table.label} tem a comanda {table.local_number} aberta. "
                "Receba ou cancele a conta antes de tirar a mesa do mapa."
            )
        if table.is_active == active:
            return table

        try:
            with self._db.transaction() as connection:
                connection.execute(
                    "UPDATE store_tables SET is_active = ?, updated_at = ?, "
                    "is_synced = 0 WHERE id = ? AND tenant_id = ?",
                    (int(active), iso(utc_now()), table_id, self._config.tenant_id),
                )
                self._enqueue(
                    connection, str(table_id), new_id(), "update",
                    {"is_active": active},
                )
        except sqlite3.IntegrityError as exc:
            # Reativar esbarra no índice único: o rótulo foi reaproveitado por
            # outra mesa enquanto esta estava fora do mapa.
            raise TableError(
                f"Já existe outra mesa ativa chamada {table.label!r}. "
                "Renomeie uma das duas antes de reativar."
            ) from exc

        return self.get(table_id)

    def seed_default_tables(self, count: int = 12, *, area: str = "Salão") -> int:
        """Cria "Mesa 1".."Mesa N" de uma vez, pulando as que já existem.

        O primeiro dia de uso não pode começar com uma tela vazia e um botão de
        cadastro: o garçom precisa lançar pedido, não configurar sistema.
        """
        created = 0
        for number in range(1, max(0, int(count)) + 1):
            label = f"Mesa {number}"
            if self.find_by_label(label) is not None:
                continue
            self.create(label=label, area=area, seats=4, sort_order=number)
            created += 1
        return created

    # -- internos ------------------------------------------------------------- #

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        table_id: str,
        client_uuid: str,
        operation: str,
        payload: dict[str, object],
    ) -> None:
        self._outbox.enqueue(
            connection,
            entity_table="store_tables",
            entity_id=EntityId(table_id),
            client_uuid=EntityId(client_uuid),
            operation=operation,
            # `updated_at` decide, na nuvem, qual de duas mudanças é a mais nova:
            # um lote antigo reenviado não desfaz o rótulo de hoje.
            payload={"id": table_id, **payload, "updated_at": iso(utc_now())},
        )


def _clean(value: str, message: str, limit: int) -> str:
    cleaned = " ".join(str(value).split())[:limit]
    if not cleaned:
        raise TableError(message)
    return cleaned


def _to_table(row: sqlite3.Row) -> StoreTable:
    order_id = row["order_id"]
    return StoreTable(
        id=EntityId(str(row["id"])),
        label=str(row["label"]),
        area=str(row["area"]),
        seats=int(row["seats"]),
        sort_order=int(row["sort_order"]),
        is_active=bool(row["is_active"]),
        order_id=EntityId(str(order_id)) if order_id else None,
        local_number=int(row["local_number"]) if order_id else None,
        total_cents=Cents(int(row["order_total"] or 0)),
        item_count=int(row["items"] or 0),
        opened_at=str(row["opened_at"]) if order_id else None,
        bill_requested_at=(
            str(row["bill_requested_at"]) if row["bill_requested_at"] else None
        ),
    )


__all__ = ["MAX_TABLES", "StoreTable", "TableError", "TableService"]
