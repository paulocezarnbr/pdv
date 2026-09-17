"""Motor de sincronização — o ciclo push/pull.

Esta classe é deliberadamente **síncrona e sem threads**. Toda a política de
concorrência vive em `worker.py`. Assim o comportamento crítico — o que
acontece quando a rede cai exatamente entre o commit do servidor e a resposta —
é testável de forma determinística, sem `sleep` nem corrida de threads no teste.

A regra que sustenta o "zero duplicidade, zero perda":

    O cliente NUNCA decide que algo foi sincronizado.
    Só o ACK do servidor decide. Na dúvida, reenvia.

Reenviar é seguro porque `client_uuid` é chave de idempotência no servidor.
Não reenviar seria perda permanente. Diante da incerteza, a escolha é sempre
reenviar.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.sync.outbox import CursorStore, OutboxReader
from pdv.sync.protocol import (
    AuthError,
    ItemStatus,
    PullRequest,
    PushBatch,
    SyncReport,
    Transport,
    TransportError,
)

logger = logging.getLogger(__name__)

#: Tabelas de cadastro que descem da retaguarda para o PDV.
PULLABLE_TABLES: tuple[str, ...] = (
    "products",
    "recipes",
    "recipe_lines",
    "inventory_items",
    "users",
)


class SyncEngine:
    """Executa ciclos de sincronização contra a nuvem."""

    def __init__(
        self,
        database: Database,
        transport: Transport,
        config: AppConfig,
        batch_size: int = 200,
    ) -> None:
        self._db = database
        self._transport = transport
        self._config = config
        self._batch_size = batch_size
        self._reader = OutboxReader(database)
        self._cursors = CursorStore(database)

    # -- envio ---------------------------------------------------------------- #

    def push_once(self) -> SyncReport:
        """Envia um lote. Retorna o relatório do que aconteceu com ele."""
        items = self._reader.claim_batch(self._batch_size)
        if not items:
            return SyncReport()

        batch = PushBatch(
            device_id=self._config.device_id,
            tenant_id=self._config.tenant_id,
            store_id=self._config.store_id,
            items=tuple(items),
        )

        try:
            response = self._transport.push(batch)
        except AuthError as exc:
            # Credencial revogada: reenviar não resolve, mas os dados também não
            # podem ser descartados. Ficam na fila com backoff até um humano
            # reativar o terminal.
            logger.error("Sincronização bloqueada por autenticação: %s", exc)
            self._reader.defer(items, f"auth: {exc}")
            return SyncReport(
                sent=len(items), deferred=len(items), error=f"auth: {exc}"
            )
        except TransportError as exc:
            # O caso perigoso mora aqui: a requisição PODE ter sido aplicada no
            # servidor e a resposta ter se perdido. Por isso nada sai da fila —
            # o reenvio vai receber `duplicate` e fechar o ciclo corretamente.
            logger.warning("Falha de transporte, lote volta à fila: %s", exc)
            self._reader.defer(items, str(exc))
            return SyncReport(sent=len(items), deferred=len(items), error=str(exc))

        acks = response.by_uuid()

        settled = self._reader.settle(items, acks)

        rejected = [
            item
            for item in items
            if (ack := acks.get(item.client_uuid)) is not None
            and ack.status is ItemStatus.REJECTED
        ]
        if rejected:
            # Rejeição é decisão do servidor: reenviar o mesmo payload dá o
            # mesmo resultado. Vai para quarentena, sai do caminho da fila e
            # espera intervenção — mas não é apagado.
            messages = "; ".join(
                f"{item.entity_table}#{item.client_uuid[:8]}: "
                f"{acks[item.client_uuid].message}"
                for item in rejected
            )
            logger.error("Itens rejeitados pelo servidor: %s", messages)
            self._reader.quarantine(rejected, messages)

        # Itens sem veredito (o servidor respondeu o lote mas omitiu este item)
        # voltam à fila. Silêncio nunca é interpretado como sucesso.
        unanswered = [item for item in items if item.client_uuid not in acks]
        if unanswered:
            logger.warning("%d item(ns) sem resposta do servidor", len(unanswered))
            self._reader.defer(unanswered, "sem veredito do servidor")

        return SyncReport(
            sent=len(items),
            settled=settled,
            rejected=len(rejected),
            deferred=len(unanswered),
        )

    def drain(self, max_cycles: int = 100) -> SyncReport:
        """Envia lotes até esvaziar a fila, falhar ou atingir o teto de ciclos.

        O teto existe para o worker não ficar preso num lote que sempre falha:
        ele devolve o controle, dorme e tenta de novo no próximo ciclo.
        """
        total = SyncReport()

        for _ in range(max_cycles):
            report = self.push_once()
            total = replace(
                total,
                sent=total.sent + report.sent,
                settled=total.settled + report.settled,
                rejected=total.rejected + report.rejected,
                deferred=total.deferred + report.deferred,
                error=report.error or total.error,
            )
            if report.sent == 0 or report.error is not None:
                break

        return total

    # -- recebimento ---------------------------------------------------------- #

    def pull_once(self) -> int:
        """Baixa alterações de cadastro feitas na retaguarda.

        Cadastros usam LWW (`updated_at`), então aplicar duas vezes a mesma
        linha é inofensivo. Por isso o pull não precisa da maquinaria de
        idempotência do push — só de um cursor por tabela.
        """
        applied = 0

        for table in PULLABLE_TABLES:
            cursor = self._cursors.get(table)
            try:
                response = self._transport.pull(
                    PullRequest(
                        tenant_id=self._config.tenant_id,
                        store_id=self._config.store_id,
                        entity_table=table,
                        since_server_seq=cursor,
                    )
                )
            except (TransportError, AuthError) as exc:
                logger.warning("Pull de %s falhou: %s", table, exc)
                break

            if not response.rows:
                continue

            applied += self._apply_pulled_rows(table, response.rows)
            self._cursors.set(table, response.last_server_seq)

        return applied

    def _apply_pulled_rows(self, table: str, rows: tuple[dict[str, object], ...]) -> int:
        """Aplica linhas de cadastro com UPSERT, numa transação por tabela."""
        if table not in PULLABLE_TABLES:
            return 0

        applied = 0
        with self._db.transaction() as connection:
            for row in rows:
                columns = sorted(row.keys())
                placeholders = ", ".join("?" for _ in columns)
                column_list = ", ".join(columns)
                # Conflito no PK resolve por LWW: a retaguarda é a autoridade
                # sobre cadastro, então a linha que desce sempre vence.
                updates = ", ".join(
                    f"{c} = excluded.{c}" for c in columns if c != "id"
                )
                connection.execute(
                    f"INSERT INTO {table} ({column_list}) "  # noqa: S608
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (id) DO UPDATE SET {updates}",
                    tuple(row[c] for c in columns),
                )
                applied += 1

        return applied

    # -- estado --------------------------------------------------------------- #

    def pending_count(self) -> int:
        return self._reader.pending_count()

    def quarantined_count(self) -> int:
        return self._reader.dead_letter_count()
