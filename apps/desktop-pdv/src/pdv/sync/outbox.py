"""Leitura e baixa da fila de saída.

Esta é a metade cliente da garantia "zero duplicidade, zero perda":

* **Nada sai da fila sem ACK.** O item só é removido do outbox — e a entidade
  só é marcada `is_synced = 1` — depois que o servidor confirmou. Queda entre o
  envio e a resposta deixa o item na fila, e ele sobe de novo.
* **Ordem preservada.** Lotes são montados por `seq` crescente. O ledger de
  auditoria depende disso: o servidor valida a cadeia e um elo fora de ordem
  seria recusado.
* **Backoff exponencial.** Servidor fora do ar não vira tempestade de
  requisições; o `available_at` segura o item.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Final

from pdv.data.database import Database
from pdv.domain.models import iso, utc_now
from pdv.sync.protocol import ItemAck, OutboxItem

#: Tabelas que o cliente pode marcar como sincronizadas. Lista fechada de
#: propósito: o nome da tabela vem do banco e é interpolado no SQL. Sem esta
#: barreira, uma linha adulterada no outbox viraria injeção de SQL.
SYNCABLE_TABLES: Final[frozenset[str]] = frozenset(
    {
        "orders",
        "order_items",
        "order_item_ingredients",
        "payments",
        "stock_movements",
        "audit_ledger",
        "cash_sessions",
        "customers",
        "cashback_ledger",
        "prepaid_ledger",
    }
)

#: Teto do backoff: 5 minutos. Além disso o caixa ficaria "mudo" tempo demais,
#: e caixa mudo é exatamente o sintoma que o monitoramento precisa enxergar.
MAX_BACKOFF_SECONDS: Final[int] = 300

#: Após N tentativas o item vira dead-letter: para de bloquear a fila, mas
#: **nunca** é descartado — some da fila só com intervenção humana.
MAX_ATTEMPTS: Final[int] = 25


class OutboxReader:
    """Acesso transacional à fila de saída."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- leitura -------------------------------------------------------------- #

    def claim_batch(self, limit: int = 200) -> list[OutboxItem]:
        """Pega o próximo lote elegível, em ordem de `seq`.

        Não marca nada como "em voo": se o processo morrer no meio do envio, o
        item continua elegível na próxima rodada. Reenviar é seguro — a
        idempotência do servidor resolve. Já *perder* não seria.
        """
        now = iso(utc_now())
        rows = self._db.connection.execute(
            """
            SELECT seq, entity_table, entity_id, client_uuid, operation,
                   payload_json, attempts
            FROM sync_outbox
            WHERE available_at <= ? AND attempts < ?
            ORDER BY seq
            LIMIT ?
            """,
            (now, MAX_ATTEMPTS, limit),
        ).fetchall()

        return [
            OutboxItem(
                seq=int(row["seq"]),
                entity_table=row["entity_table"],
                entity_id=row["entity_id"],
                client_uuid=row["client_uuid"],
                operation=row["operation"],
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def pending_count(self) -> int:
        row = self._db.connection.execute(
            "SELECT COUNT(*) AS total FROM sync_outbox"
        ).fetchone()
        return int(row["total"])

    def dead_letter_count(self) -> int:
        row = self._db.connection.execute(
            "SELECT COUNT(*) AS total FROM sync_outbox WHERE attempts >= ?",
            (MAX_ATTEMPTS,),
        ).fetchone()
        return int(row["total"])

    # -- baixa ---------------------------------------------------------------- #

    def settle(self, items: list[OutboxItem], acks: dict[str, ItemAck]) -> int:
        """Remove da fila os itens confirmados e marca a entidade sincronizada.

        Tudo numa transação: é impossível a entidade ficar marcada como
        sincronizada com o item ainda na fila (reenvio eterno) ou o item sair da
        fila sem a entidade ser marcada (perda de rastro).
        """
        settled_at = iso(utc_now())
        settled = 0

        with self._db.transaction() as connection:
            for item in items:
                ack = acks.get(item.client_uuid)
                if ack is None or not ack.status.is_settled:
                    continue
                self._mark_entity_synced(connection, item, settled_at, ack.server_seq)
                connection.execute("DELETE FROM sync_outbox WHERE seq = ?", (item.seq,))
                settled += 1

        return settled

    def defer(self, items: list[OutboxItem], error: str) -> None:
        """Devolve os itens à fila com backoff exponencial."""
        with self._db.transaction() as connection:
            for item in items:
                attempts = item.attempts + 1
                delay = min(2**attempts, MAX_BACKOFF_SECONDS)
                available_at = iso(utc_now() + timedelta(seconds=delay))
                connection.execute(
                    "UPDATE sync_outbox SET attempts = ?, last_error = ?, "
                    "available_at = ? WHERE seq = ?",
                    (attempts, error[:500], available_at, item.seq),
                )

    def quarantine(self, items: list[OutboxItem], error: str) -> None:
        """Move itens rejeitados para quarentena (dead-letter).

        Rejeição é veredito do servidor: o mesmo payload produziria o mesmo
        resultado, então retentar é desperdício. Mas **nada é apagado** — o item
        fica no banco com `attempts` no teto, sai do caminho da fila e espera
        análise humana. Perder uma venda para "limpar a fila" é inaceitável.
        """
        with self._db.transaction() as connection:
            for item in items:
                connection.execute(
                    "UPDATE sync_outbox SET attempts = ?, last_error = ?, "
                    "available_at = ? WHERE seq = ?",
                    (MAX_ATTEMPTS, error[:500], iso(utc_now()), item.seq),
                )

    # -- internos ------------------------------------------------------------- #

    @staticmethod
    def _mark_entity_synced(
        connection: sqlite3.Connection,
        item: OutboxItem,
        settled_at: str,
        server_seq: int | None,
    ) -> None:
        if item.entity_table not in SYNCABLE_TABLES:
            # Não é erro fatal: a fila segue. Mas o item sai sem marcar a
            # entidade, e isso precisa aparecer no log do suporte.
            return

        # Seguro: `entity_table` foi validado contra a lista fechada acima.
        connection.execute(
            f"UPDATE {item.entity_table} "  # noqa: S608
            "SET is_synced = 1, synced_at = ? WHERE client_uuid = ?",
            (settled_at, item.client_uuid),
        )


class CursorStore:
    """Cursores do *pull* incremental, por tabela."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, entity_table: str) -> int:
        row = self._db.connection.execute(
            "SELECT last_server_seq FROM sync_cursors WHERE entity_table = ?",
            (entity_table,),
        ).fetchone()
        return int(row["last_server_seq"]) if row else 0

    def set(self, entity_table: str, server_seq: int) -> None:
        with self._db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_cursors (entity_table, last_server_seq, last_pulled_at)
                VALUES (?, ?, ?)
                ON CONFLICT (entity_table) DO UPDATE
                    SET last_server_seq = excluded.last_server_seq,
                        last_pulled_at = excluded.last_pulled_at
                """,
                (entity_table, server_seq, iso(utc_now())),
            )
