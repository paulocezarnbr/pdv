"""Aplicação dos comandos vindos do painel administrativo.

> ⚠️ Este módulo **inverte o modelo de confiança** do sistema. Até a Fase 3 o
> PDV só mandava dados para fora. Aceitar ordens de fora transforma o terminal
> em alvo: quem comprometer o painel passa a conceder descontos e cancelar
> itens em todas as lojas ao mesmo tempo — e sem pisar em nenhuma delas.

As sete travas, e o que cada uma impede
---------------------------------------

1. **Assinatura HMAC por terminal** (`protocol.py`). Autenticar a *conexão*
   prova que alguém tem a credencial do terminal; assinar o *comando* prova que
   a nuvem emitiu aquele comando para aquele terminal.
2. **Janela de validade.** Comando capturado do canal e reaplicado semanas
   depois não tem efeito.
3. **Teto do perfil de quem emitiu.** Estar longe não amplia poder: se o
   gerente não pode conceder 50% no balcão, não pode pelo painel. E o teto é
   lido da réplica local de `users` — não do que o comando afirma, senão a
   trava seria conferida contra o número que o atacante escolheu.
4. **Escopo em pedido aberto.** O painel nunca reescreve o passado. Venda
   fechada se corrige por estorno; documento fiscal transmitido, por
   cancelamento fiscal.
5. **Idempotência por `command_uuid`** (`inbox.py`). Reenvio por timeout não
   concede o desconto duas vezes.
6. **Chave de desligamento local.** O dono desliga o canal pelo terminal, e o
   terminal para de obedecer — sem depender de a nuvem cooperar, que é
   justamente o que não se pode supor quando o painel é o que foi comprometido.
7. **Aceite presencial para o que já saiu da cozinha.** Cancelar de longe um
   item que a cozinha já recebeu é a rota limpa do furto de salão: o prato sai,
   alguém fora da loja cancela, a conta fecha menor. As seis travas acima não
   veem nada de errado — o gerente existe, está dentro do teto e assinou. O que
   falta é alguém **na loja** olhando para a mesa. O comando para e espera o
   login e o PIN de quem está no caixa (`confirm` / `decline`); até lá continua
   `pending`, sujeito às mesmas travas a cada ciclo, e vence na mesma janela.

Abrir gaveta não precisa dessa trava porque não existe: o terminal não aceita
esse comando de fora de jeito nenhum (ver `CommandKind`).

Toda recusa vira evento no ledger. Recusa é informação de segurança: ninguém
emite por acidente um desconto acima do próprio teto, e o padrão de tentativas
recusadas é o que denuncia a credencial vazada antes do prejuízo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository, SaleRepository, StockRepository
from pdv.data.settings import SettingsStore
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    AuditEventType,
    AuditSeverity,
    Cents,
    EntityId,
    Milligrams,
    iso,
    utc_now,
)
from pdv.edge.hub import Event, EventHub
from pdv.edge.kds import KdsService
from pdv.hardware.printer.escpos import format_cents
from pdv.remote.inbox import AwaitingCommand, InboxRepository
from pdv.remote.protocol import (
    CommandKind,
    CommandStatus,
    RemoteCommand,
    is_fresh,
    verify_signature,
)
from pdv.services.audit import AuditService
from pdv.services.authorization import AuthorizationService, Identity

logger = logging.getLogger(__name__)

#: Chave de desligamento do canal remoto. Ausente significa **ligado**: uma loja
#: que nunca abriu a tela de configuração ainda recebe comando do painel. O
#: valor "0" desliga.
REMOTE_ENABLED_KEY = "remote.commands_enabled"

#: Canal gravado na auditoria. O relatório de cancelamentos separa presencial de
#: remoto por este campo. Sem a separação, o painel viraria a rota limpa para o
#: mesmo furto que o módulo anti-fraude existe para combater.
CHANNEL = "remote_panel"

#: Quem pode dar o aceite presencial. É gente do **balcão**: o garçom fica de
#: fora porque, no furto de salão, é ele quem leva o prato — e o aceite dele
#: sobre o cancelamento remoto do próprio item fecharia o circuito sem mais
#: ninguém olhando.
CONFIRMER_ROLES: frozenset[str] = frozenset({"cashier", "manager", "owner"})

#: Como a cozinha está com o item, na língua de quem vai decidir.
_KITCHEN_LABELS: dict[str, str] = {
    "queued": "já na fila da cozinha",
    "preparing": "em preparo na cozinha",
    "ready": "pronto na cozinha",
    "delivered": "já entregue à mesa",
}


class CommandRefused(Exception):
    """O comando foi recusado.

    A `severity` distingue o que é engano de operação (um gerente pedindo mais
    do que pode) do que é ataque ou defeito grave (assinatura inválida). As
    duas coisas viram evento, mas só a segunda precisa acordar alguém.
    """

    def __init__(
        self, message: str, severity: AuditSeverity = AuditSeverity.WARNING
    ) -> None:
        super().__init__(message)
        self.severity = severity


class ConfirmationError(PdvError):
    """O aceite no caixa não foi aceito — e isso **não** decide o comando.

    Credencial errada, papel que não pode dar aceite, comando que já foi
    decidido noutro lugar. O comando continua como estava; transformar um PIN
    digitado errado em recusa definitiva deixaria o caixa desfazer, por
    engano de digitação, o que o gerente mandou.
    """


class _AlreadySettled(Exception):
    """Outra execução fechou o comando primeiro — a transação inteira volta."""


class _NeedsConfirmation(Exception):
    """O comando é legítimo, mas só vale com alguém presente no caixa."""

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


@dataclass(frozen=True, slots=True)
class ApplyReport:
    applied: int = 0
    refused: int = 0
    deferred: int = 0
    #: Esperando o aceite de alguém no caixa. Não é falha nem adiamento por
    #: defeito: é a trava 7 funcionando.
    awaiting: int = 0

    @property
    def total(self) -> int:
        return self.applied + self.refused + self.deferred + self.awaiting


class RemoteCommandService:
    """Aplica os comandos pendentes da inbox, um a um."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        checkout: Any | None = None,
        hub: EventHub | None = None,
    ) -> None:
        """
        Args:
            checkout: o `CheckoutService` vivo deste terminal, quando houver.

                Um comando pode mirar o pedido que o caixa tem **aberto na
                tela**. Mexer no banco por baixo dele deixaria a venda em
                memória desatualizada, e o `finalize_sale` gravaria o total
                antigo por cima — o desconto do gerente sumiria no instante
                exato em que o cliente fosse pagar. Com o serviço em mãos, o
                objeto em memória é acertado junto; sem ele (uso headless, sem
                UI), só o banco.
            hub: o barramento do servidor do salão, quando ele está no ar. O
                item cancelado some da tela da cozinha na hora; sem o aviso, a
                cozinha continuaria preparando um prato que ninguém vai pagar
                até alguém recarregar o KDS.
        """
        self._db = database
        self._config = config
        self._checkout = checkout
        self._hub = hub
        self._inbox = InboxRepository(database)
        self._outbox = OutboxRepository()
        self._settings = SettingsStore(database)

    # -- ciclo ---------------------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        return self._settings.get(REMOTE_ENABLED_KEY, "1") != "0"

    def apply_pending(self, limit: int = 50) -> ApplyReport:
        """Aplica os comandos pendentes. Cada um na sua própria transação."""
        commands = self._inbox.pending(limit)
        if not commands:
            return ApplyReport()

        applied = refused = deferred = awaiting = 0
        for command in commands:
            try:
                message = self._apply_one(command)
            except _NeedsConfirmation as exc:
                if self._inbox.request_confirmation(command.command_uuid, exc.note):
                    logger.warning(
                        "Comando %s espera aceite no caixa: %s",
                        command.command_uuid,
                        exc.note,
                    )
                awaiting += 1
            except CommandRefused as exc:
                try:
                    self._refuse(command, str(exc), exc.severity)
                except _AlreadySettled:
                    deferred += 1
                else:
                    refused += 1
            except _AlreadySettled:
                # Corrida com outra execução do ciclo. Quem chegou primeiro já
                # decidiu; aqui não há nada a fazer e nada a registrar.
                deferred += 1
            except Exception:  # noqa: BLE001
                # Falha inesperada não pode virar recusa definitiva: o comando é
                # legítimo e o problema é nosso. Continua pendente e tenta de
                # novo no próximo ciclo.
                logger.exception("Falha ao aplicar comando %s", command.command_uuid)
                deferred += 1
            else:
                logger.info("Comando %s aplicado: %s", command.command_uuid, message)
                applied += 1

        return ApplyReport(
            applied=applied, refused=refused, deferred=deferred, awaiting=awaiting
        )

    # -- aceite no caixa ------------------------------------------------------ #

    def awaiting(self, limit: int = 50) -> list[AwaitingCommand]:
        """O que está parado na frente do caixa esperando alguém decidir."""
        return self._inbox.awaiting_confirmation(limit)

    def confirmer_logins(self) -> list[str]:
        """Logins que podem dar o aceite, para preencher o diálogo do caixa."""
        placeholders = ",".join("?" for _ in CONFIRMER_ROLES)
        rows = self._db.query_all(
            "SELECT login FROM users "
            f" WHERE tenant_id = ? AND is_active = 1 AND role IN ({placeholders}) "
            " ORDER BY name",
            (self._config.tenant_id, *sorted(CONFIRMER_ROLES)),
        )
        return [str(row["login"]) for row in rows]

    def confirm(self, command_uuid: str, *, login: str, pin: str) -> str:
        """Aplica um comando que esperava aceite, com a credencial de quem aceita.

        A credencial é conferida **aqui**, e não no diálogo: o diálogo pode ser
        trocado, chamado por outro caminho ou esquecido numa tela nova, e a
        trava 7 não pode depender de nenhuma tela lembrar dela.

        Todas as outras travas rodam de novo. Entre o pedido e o aceite o
        pedido pode ter fechado, o gerente pode ter sido desativado e a janela
        pode ter vencido — aceitar não ressuscita o que deixou de valer.

        Raises:
            ConfirmationError: credencial ou papel inválidos, ou o comando não
                espera mais aceite. O comando continua como estava.
            CommandRefused: uma das travas recusou. O comando fica recusado, e
                a recusa vai para o ledger como qualquer outra.
        """
        confirmer = self._confirmer(login, pin)
        command = self._awaiting_command(command_uuid)

        try:
            message = self._apply_one(command, confirmed_by=confirmer)
        except _NeedsConfirmation as exc:  # pragma: no cover - defesa
            raise ConfirmationError("O comando ainda não pôde ser aplicado.") from exc
        except CommandRefused as exc:
            try:
                self._refuse(command, str(exc), exc.severity)
            except _AlreadySettled as settled:
                raise ConfirmationError("Este comando já foi decidido.") from settled
            raise
        except _AlreadySettled as exc:
            raise ConfirmationError("Este comando já foi decidido.") from exc

        logger.info(
            "Comando %s aplicado com aceite de %s: %s",
            command.command_uuid,
            confirmer.name,
            message,
        )
        return message

    def decline(
        self, command_uuid: str, *, login: str, pin: str, reason: str
    ) -> None:
        """Recusa, no caixa, um comando que esperava aceite.

        A recusa é **definitiva** e volta para o painel com o nome de quem
        recusou e o motivo. "Recusado" sem motivo faz o gerente emitir de novo
        igual — e desta vez talvez para um caixa sem ninguém prestando atenção.

        Raises:
            ConfirmationError: credencial ou papel inválidos, motivo vazio, ou
                o comando não espera mais aceite.
        """
        reason = " ".join(str(reason).split())[:200]
        if not reason:
            raise ConfirmationError("Recusar exige motivo — ele volta para o painel.")

        confirmer = self._confirmer(login, pin)
        command = self._awaiting_command(command_uuid)

        try:
            self._refuse(
                command,
                f"Recusado no caixa por {confirmer.name}: {reason}",
                AuditSeverity.WARNING,
                extra={
                    "declined_by_user_id": str(confirmer.id),
                    "declined_by_name": confirmer.name,
                },
            )
        except _AlreadySettled as exc:
            raise ConfirmationError("Este comando já foi decidido.") from exc

    def _confirmer(self, login: str, pin: str) -> Identity:
        identity = AuthorizationService(self._db, self._config.tenant_id).authenticate(
            login, pin
        )
        if identity.role not in CONFIRMER_ROLES:
            raise ConfirmationError(
                "O aceite de comando do painel exige alguém do caixa: operador, "
                "gerente ou proprietário."
            )
        return identity

    def _awaiting_command(self, command_uuid: str) -> RemoteCommand:
        command = self._inbox.get_pending(command_uuid)
        if command is None:
            raise ConfirmationError("Este comando já foi decidido.")
        if not self._inbox.is_awaiting(command_uuid):
            # Só o que o terminal pôs para esperar pode ser aceito. Aceitar
            # qualquer pendente pela porta do caixa seria pular a avaliação
            # que decide se ele precisa de aceite — e aplicar na tela algo que
            # o ciclo normal ainda nem conferiu.
            raise ConfirmationError("Este comando não está esperando aceite.")
        return command

    # -- aplicação ------------------------------------------------------------ #

    def _apply_one(
        self, command: RemoteCommand, *, confirmed_by: Identity | None = None
    ) -> str:
        self._check_admissible(command)

        if command.kind is CommandKind.APPLY_DISCOUNT:
            return self._apply_discount(command)
        if command.kind is CommandKind.CANCEL_ITEM:
            return self._cancel_item(command, confirmed_by=confirmed_by)
        raise CommandRefused(f"Comando desconhecido: {command.kind.value}")

    def _check_admissible(self, command: RemoteCommand) -> None:
        """As travas que não dependem do tipo do comando."""
        if not self.enabled:
            raise CommandRefused("Canal de comando remoto desligado neste terminal.")

        if command.tenant_id != self._config.tenant_id:
            raise CommandRefused("Comando de outra rede.", AuditSeverity.CRITICAL)

        if command.device_id != self._config.device_id:
            # Comando endereçado a outro terminal chegando aqui é replay ou erro
            # de roteamento. Nos dois casos, não se obedece.
            raise CommandRefused(
                "Comando endereçado a outro terminal.", AuditSeverity.CRITICAL
            )

        if not verify_signature(command, self._config.device_secret):
            # O único caso aqui que nunca é engano de operação.
            raise CommandRefused("Assinatura inválida.", AuditSeverity.CRITICAL)

        if not is_fresh(command.issued_at):
            raise CommandRefused("Comando fora da janela de validade — emita novamente.")

    def _apply_discount(self, command: RemoteCommand) -> str:
        order_id = _text(command.payload, "order_id")
        percent = _percent(command.payload, "percent")
        reason = _text(command.payload, "reason", "Desconto remoto exige motivo.")

        ceiling = self._discount_ceiling(command.issued_by_user_id)
        if percent > ceiling:
            raise CommandRefused(
                f"{command.issued_by_name} pode conceder até {_plain(ceiling)}% — "
                f"o comando pede {_plain(percent)}%."
            )

        order = self._require_open_order(order_id)
        subtotal = Cents(int(order["subtotal_cents"]))
        discount = Cents(
            int((Decimal(int(subtotal)) * percent / Decimal(100)).quantize(Decimal("1")))
        )
        total = Cents(max(0, int(subtotal) - int(discount)))

        with self._db.transaction() as connection:
            self._claim(connection, command)
            SaleRepository(connection, self._outbox).update_totals(
                EntityId(order_id), subtotal, discount, total
            )
            self._audit(
                connection,
                command,
                event_type=AuditEventType.DISCOUNT_APPLIED,
                severity=AuditSeverity.WARNING,
                payload={
                    "order_id": order_id,
                    "percent": _plain(percent),
                    "subtotal_cents": int(subtotal),
                    "discount_cents": int(discount),
                    "total_cents": int(total),
                    "reason": reason,
                },
            )

        # Só depois do commit. Espelhar antes deixaria a tela mostrando um
        # desconto que o banco recusou.
        live = self._live_sale_for(order_id)
        if live is not None:
            live.discount_cents = discount

        return f"desconto de {_plain(percent)}% (R$ {int(discount) / 100:.2f})"

    def _cancel_item(
        self, command: RemoteCommand, *, confirmed_by: Identity | None = None
    ) -> str:
        order_id = _text(command.payload, "order_id")
        item_id = _text(command.payload, "order_item_id")
        reason = _text(command.payload, "reason", "Cancelamento remoto exige motivo.")

        self._require_authorizer(
            command.issued_by_user_id, allowed_roles=frozenset({"manager"})
        )
        order = self._require_open_order(order_id)

        item = self._db.query_one(
            "SELECT id, product_name, total_cents, canceled_at FROM order_items "
            " WHERE id = ? AND order_id = ?",
            (item_id, order_id),
        )
        if item is None:
            raise CommandRefused("Item não encontrado neste pedido.")
        if item["canceled_at"] is not None:
            raise CommandRefused("O item já estava cancelado.")

        kitchen = self._kitchen_status(item_id)
        if kitchen is not None and confirmed_by is None:
            raise _NeedsConfirmation(
                self._confirmation_note(command, order, item, kitchen, reason)
            )

        consumptions = self._db.query_all(
            "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients "
            " WHERE order_item_id = ?",
            (item_id,),
        )

        with self._db.transaction() as connection:
            self._claim(connection, command)
            SaleRepository(connection, self._outbox).cancel_item(
                EntityId(item_id),
                canceled_at=iso(utc_now()),
                canceled_by_user_id=EntityId(command.issued_by_user_id),
                reason=f"[{CHANNEL}] {reason}",
            )

            # O ticket sai da fila junto com o item, na mesma transação. Deixá-lo
            # para trás mandaria a cozinha preparar um prato que já não está na
            # conta — e ele sairia da cozinha de graça.
            tickets = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM kds_tickets "
                    " WHERE order_item_id = ? AND status <> 'canceled'",
                    (item_id,),
                ).fetchall()
            ]
            connection.execute(
                "UPDATE kds_tickets SET status = 'canceled', updated_at = ? "
                " WHERE order_item_id = ? AND status <> 'canceled'",
                (iso(utc_now()), item_id),
            )

            stock = StockRepository(connection, self._outbox)
            for line in consumptions:
                stock.register_movement(
                    tenant_id=EntityId(self._config.tenant_id),
                    store_id=EntityId(self._config.store_id),
                    device_id=EntityId(self._config.device_id),
                    inventory_item_id=EntityId(str(line["inventory_item_id"])),
                    qty_mg=Milligrams(int(line["consumed_mg"])),  # positivo = estorno
                    movement_type="adjustment",
                    reference_type="order_item_cancel",
                    reference_id=EntityId(item_id),
                )

            discount = self._recalculate_totals(connection, order_id)
            payload: dict[str, Any] = {
                "order_id": order_id,
                "order_item_id": item_id,
                "product_name": str(item["product_name"]),
                "total_cents": int(item["total_cents"]),
                "reason": reason,
            }
            if kitchen is not None:
                payload["kitchen_status"] = kitchen
            if confirmed_by is not None:
                # A terceira identidade. Quem mandou está no painel; quem
                # estava na loja e concordou está aqui — e é a primeira pessoa
                # a quem se pergunta, depois, o que aconteceu naquela mesa.
                payload["confirmed_by_user_id"] = str(confirmed_by.id)
                payload["confirmed_by_name"] = confirmed_by.name
            self._audit(
                connection,
                command,
                event_type=AuditEventType.ITEM_CANCELED,
                severity=AuditSeverity.CRITICAL,
                payload=payload,
            )

        live = self._live_sale_for(order_id)
        if live is not None:
            live.items[:] = [i for i in live.items if str(i.id) != item_id]
            live.discount_cents = discount

        self._announce_kitchen(tickets)

        message = f"item {item['product_name']} cancelado"
        if confirmed_by is not None:
            message += f" com aceite de {confirmed_by.name} no caixa"
        return message

    # -- travas --------------------------------------------------------------- #

    def _claim(self, connection: Any, command: RemoteCommand) -> None:
        """Marca o comando como aplicado **dentro** da transação do efeito.

        Se o `UPDATE ... WHERE status = 'pending'` não pegar nenhuma linha,
        outra execução já decidiu este comando — e a exceção desfaz o efeito
        que esta transação tinha acabado de gravar.
        """
        if not self._inbox.settle_in(
            connection, command.command_uuid, CommandStatus.APPLIED, "aplicado"
        ):
            raise _AlreadySettled

    def _require_open_order(self, order_id: str) -> Any:
        order = self._db.query_one(
            "SELECT id, status, subtotal_cents, discount_cents, channel, "
            "       customer_id FROM orders "
            " WHERE id = ? AND tenant_id = ?",
            (order_id, self._config.tenant_id),
        )
        if order is None:
            raise CommandRefused("Pedido não encontrado neste terminal.")
        if str(order["status"]) != "open":
            raise CommandRefused(
                "O pedido já foi fechado. Venda fechada se corrige por estorno, "
                "não pelo painel."
            )
        return order

    def _require_authorizer(
        self, user_id: str, *, allowed_roles: frozenset[str] | None = None
    ) -> Any:
        row = self._db.query_one(
            "SELECT name, role, can_authorize, max_discount_percent FROM users "
            " WHERE id = ? AND tenant_id = ? AND is_active = 1",
            (user_id, self._config.tenant_id),
        )
        if row is None:
            raise CommandRefused(
                "Quem emitiu o comando não existe ou está inativo neste terminal."
            )
        if not int(row["can_authorize"]):
            raise CommandRefused(
                f"{row['name']} não tem permissão para autorizar esta operação."
            )
        if allowed_roles is not None and str(row["role"]) not in allowed_roles:
            raise CommandRefused("Cancelamento de item exige autorização de gerente.")
        return row

    def _discount_ceiling(self, user_id: str) -> Decimal:
        row = self._require_authorizer(user_id)
        try:
            return Decimal(str(row["max_discount_percent"] or "0"))
        except InvalidOperation:
            # Teto ilegível vira zero, não infinito. Um campo que não dá para
            # interpretar nunca pode virar permissão para agir.
            return Decimal(0)

    # -- apoio ---------------------------------------------------------------- #

    def _kitchen_status(self, item_id: str) -> str | None:
        """Onde o item está na cozinha, ou `None` se ele nunca foi para lá.

        Ticket cancelado não conta: ele saiu da fila, e a cozinha não tem nada
        daquele item na mão.
        """
        row = self._db.query_one(
            "SELECT status FROM kds_tickets "
            " WHERE order_item_id = ? AND status <> 'canceled' "
            " ORDER BY created_at DESC LIMIT 1",
            (item_id,),
        )
        return str(row["status"]) if row else None

    @staticmethod
    def _confirmation_note(
        command: RemoteCommand, order: Any, item: Any, kitchen: str, reason: str
    ) -> str:
        # No salão, `customer_id` guarda a cópia do rótulo da mesa; no balcão
        # ele pode ser o cliente do cashback, que não se imprime numa nota.
        where = (
            str(order["customer_id"] or "").strip()
            if str(order["channel"]) == "waiter"
            else ""
        )
        state = _KITCHEN_LABELS.get(kitchen, "já enviado à cozinha")
        place = f" — {where}" if where else ""
        return (
            f"{command.issued_by_name or 'O painel'} pede cancelar "
            f"{item['product_name']} (R$ {format_cents(int(item['total_cents']))})"
            f"{place} — {state}. Motivo: {reason}"
        )

    def _announce_kitchen(self, ticket_ids: list[str]) -> None:
        """Avisa as telas da cozinha, **depois** do commit.

        Falhar aqui não desfaz o cancelamento: o KDS se reconcilia pela lista
        completa ao reconectar. O aviso é para a tela mudar agora, não é a
        fonte da verdade.
        """
        if self._hub is None or not ticket_ids:
            return
        kds = KdsService(self._db, self._config, self._hub)
        for ticket_id in ticket_ids:
            try:
                ticket = kds.get(EntityId(ticket_id))
            except Exception:  # noqa: BLE001
                logger.exception("Não foi possível avisar a cozinha: %s", ticket_id)
                continue
            self._hub.publish(Event("ticket.changed", ticket.to_json()))

    def _live_sale_for(self, order_id: str) -> Any | None:
        """A venda aberta na tela deste caixa, se for justamente esta."""
        if self._checkout is None:
            return None
        sale = getattr(self._checkout, "current_sale", None)
        return sale if sale is not None and str(sale.id) == order_id else None

    @staticmethod
    def _recalculate_totals(connection: Any, order_id: str) -> Cents:
        """Refaz os totais do pedido a partir dos itens vivos.

        O desconto em centavos foi autorizado sobre o subtotal antigo. Se um
        item sai, mantê-lo integral poderia zerar ou até inverter o total — o
        teto do subtotal é o que impede o cancelamento de virar crédito.
        """
        subtotal = int(
            connection.execute(
                "SELECT COALESCE(SUM(total_cents), 0) AS subtotal FROM order_items "
                " WHERE order_id = ? AND canceled_at IS NULL",
                (order_id,),
            ).fetchone()["subtotal"]
        )
        current = int(
            connection.execute(
                "SELECT discount_cents FROM orders WHERE id = ?", (order_id,)
            ).fetchone()["discount_cents"]
        )
        discount = min(current, subtotal)

        connection.execute(
            "UPDATE orders SET subtotal_cents = ?, discount_cents = ?, "
            "total_cents = ?, updated_at = ? WHERE id = ?",
            (subtotal, discount, subtotal - discount, iso(utc_now()), order_id),
        )
        return Cents(discount)

    def _refuse(
        self,
        command: RemoteCommand,
        message: str,
        severity: AuditSeverity,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._db.transaction() as connection:
            if not self._inbox.settle_in(
                connection, command.command_uuid, CommandStatus.REFUSED, message
            ):
                raise _AlreadySettled
            self._audit(
                connection,
                command,
                event_type=AuditEventType.REMOTE_COMMAND_REFUSED,
                severity=severity,
                payload={
                    "kind": command.kind.value,
                    "reason": message,
                    **(extra or {}),
                },
            )
        logger.warning("Comando %s recusado: %s", command.command_uuid, message)

    def _audit(
        self,
        connection: Any,
        command: RemoteCommand,
        *,
        event_type: AuditEventType,
        severity: AuditSeverity,
        payload: dict[str, Any],
    ) -> None:
        """Auditoria com **dupla identidade**.

        O ator é quem emitiu do painel; o terminal alvo e o canal vão no
        payload. Sem os dois lados, um cancelamento remoto ficaria
        indistinguível de um feito no balcão — e o relatório que o dono usa
        para achar furto perderia exatamente a distinção que interessa.
        """
        actor = EntityId(command.issued_by_user_id)
        AuditService(
            tenant_id=EntityId(self._config.tenant_id),
            store_id=EntityId(self._config.store_id),
            device_id=EntityId(self._config.device_id),
            outbox=self._outbox,
            device_secret=self._config.device_secret,
        ).append(
            connection,
            event_type=event_type,
            actor_user_id=actor,
            authorizer_user_id=actor,
            severity=severity,
            payload={
                **payload,
                "channel": CHANNEL,
                "command_uuid": command.command_uuid,
                "issued_by_name": command.issued_by_name,
                "issued_at": command.issued_at,
                "target_device_id": self._config.device_id,
            },
        )


# --------------------------------------------------------------------------- #
# Leitura do payload
# --------------------------------------------------------------------------- #


def _text(payload: dict[str, Any], key: str, message: str | None = None) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise CommandRefused(message or f"Comando sem `{key}`.")
    return value


def _percent(payload: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(payload[key]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise CommandRefused(f"Comando com `{key}` inválido.") from exc
    if not value.is_finite() or value <= 0 or value > 100:
        raise CommandRefused(f"`{key}` fora da faixa (maior que 0, até 100).")
    return value


def _plain(value: Decimal) -> str:
    """`30` em vez de `30.00`, e `12.5` sem notação científica."""
    return f"{value.normalize():f}"


__all__ = [
    "CHANNEL",
    "CONFIRMER_ROLES",
    "REMOTE_ENABLED_KEY",
    "ApplyReport",
    "CommandRefused",
    "ConfirmationError",
    "RemoteCommandService",
]
