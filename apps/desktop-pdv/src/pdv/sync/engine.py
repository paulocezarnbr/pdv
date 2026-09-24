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
from pdv.domain.models import iso, utc_now
from pdv.remote.commands import RemoteCommandService
from pdv.remote.inbox import InboxRepository
from pdv.sync.outbox import CursorStore, OutboxReader
from pdv.sync.protocol import (
    AuthError,
    CommandCycleReport,
    CommandFetch,
    CommandReport,
    ItemStatus,
    PullRequest,
    PushBatch,
    SyncReport,
    TerminalHealth,
    Transport,
    TransportError,
    speaks_commands,
    speaks_heartbeat,
)

logger = logging.getLogger(__name__)

#: Desvio de relógio que vira aviso no log do terminal (o painel usa o mesmo).
CLOCK_SKEW_WARNING_MS = 120_000

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
        *,
        commands: RemoteCommandService | None = None,
    ) -> None:
        """
        Args:
            commands: o serviço que aplica comando do painel. Opcional em dois
                sentidos, e os dois importam: sem ele o terminal só **envia**
                dados, como era até a Fase 3.5; e um transporte que não fala de
                comando (nuvem antiga) simplesmente não dispara o ciclo. Em
                nenhum dos casos a venda deixa de subir — que é a única coisa
                aqui que não pode parar.
        """
        self._db = database
        self._transport = transport
        self._config = config
        self._batch_size = batch_size
        self._commands = commands
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

    # -- comandos do painel --------------------------------------------------- #

    @property
    def speaks_commands(self) -> bool:
        """Há canal de comando **e** alguém para aplicá-los?"""
        return self._commands is not None and speaks_commands(self._transport)

    def command_cycle(self, limit: int = 50) -> CommandCycleReport:
        """Busca, aplica e relata os comandos do painel.

        A ordem é fixa e cada passo falha para o lado seguro:

        1. **Buscar.** Falha de rede aborta o ciclo sem efeito nenhum.
        2. **Gravar na inbox.** `command_uuid` repetido é no-op — a reentrega é
           o caso normal de uma rede instável, não erro.
        3. **Aplicar.** Cada comando na sua transação, com o status saindo de
           `pending` junto do efeito (ver `remote/inbox.py`).
        4. **Relatar.** Só depois. Se o relato falhar, o resultado continua na
           fila de relato e o terminal reavisa — mas **nunca** reaplica, porque
           o status já saiu de `pending`.

        Aplicar antes de relatar é o ponto que não se inverte. Relatar primeiro
        deixaria o painel dizendo "aplicado" para um desconto que o terminal
        ainda pode recusar — e o gerente iria embora confiando no que leu.
        """
        if not self.speaks_commands:
            return CommandCycleReport()

        try:
            delivery = self._transport.fetch_commands(
                CommandFetch(
                    tenant_id=self._config.tenant_id,
                    store_id=self._config.store_id,
                    device_id=self._config.device_id,
                    limit=limit,
                )
            )
        except (TransportError, AuthError) as exc:
            logger.warning("Busca de comandos falhou: %s", exc)
            return CommandCycleReport(error=str(exc))

        inbox = InboxRepository(self._db)
        accepted = sum(1 for command in delivery.commands if inbox.accept(command))

        report = self._commands.apply_pending(limit)

        reported, error = self._report_results(limit)

        return CommandCycleReport(
            fetched=len(delivery.commands),
            accepted=accepted,
            applied=report.applied,
            refused=report.refused,
            reported=reported,
            error=error,
        )

    def _report_results(self, limit: int) -> tuple[int, str | None]:
        """Avisa a nuvem do que foi decidido. Falhar aqui não desfaz nada."""
        inbox = InboxRepository(self._db)
        results = inbox.unreported(limit)
        if not results:
            return 0, None

        try:
            accepted = self._transport.report_commands(
                CommandReport(
                    tenant_id=self._config.tenant_id,
                    store_id=self._config.store_id,
                    device_id=self._config.device_id,
                    results=tuple(results),
                )
            )
        except (TransportError, AuthError) as exc:
            logger.warning("Relato de comandos falhou: %s", exc)
            return 0, str(exc)

        # Só o que a nuvem nomeou. Marcar tudo como relatado porque a chamada
        # não deu erro esconderia uma resposta parcial, e o painel ficaria
        # mostrando `pendente` num comando já aplicado — que é o estado em que
        # alguém reemite o desconto na mão.
        known = {result.command_uuid for result in results}
        confirmed = [uuid for uuid in accepted if uuid in known]
        inbox.mark_reported(confirmed)
        return len(confirmed), None

    # -- estado --------------------------------------------------------------- #

    def heartbeat(self) -> int | None:
        """Conta à nuvem como a fila está. Devolve o desvio do relógio, em ms.

        Roda **mesmo quando o envio falhou** — é quando mais importa: fila
        travada com o painel mostrando "online" foi como o defeito de
        sincronização passou despercebido. Falha aqui não é erro de venda:
        registra e segue.
        """
        if not speaks_heartbeat(self._transport):
            return None
        pending, quarantined, oldest, reason = self._reader.health()
        health = TerminalHealth(
            device_id=self._config.device_id,
            tenant_id=self._config.tenant_id,
            terminal_clock=iso(utc_now()),
            pending_items=pending,
            quarantined_items=quarantined,
            oldest_pending_at=oldest,
            last_quarantine_reason=reason[:300] if reason else None,
        )
        try:
            drift = self._transport.heartbeat(health)  # type: ignore[attr-defined]
        except (TransportError, AuthError) as exc:
            logger.info("Relato de saúde não enviado: %s", exc)
            return None
        if abs(drift) > CLOCK_SKEW_WARNING_MS:
            logger.warning(
                "Relógio do caixa %+d s em relação à nuvem: a hora das vendas "
                "sai errada nos relatórios.", drift // 1000,
            )
        return drift

    def pending_count(self) -> int:
        return self._reader.pending_count()

    def quarantined_count(self) -> int:
        return self._reader.dead_letter_count()

    def pending_commands(self) -> int:
        return InboxRepository(self._db).pending_count()
