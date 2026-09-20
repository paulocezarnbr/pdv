"""Aplicação dos comandos vindos do painel administrativo.

> ⚠️ Este módulo **inverte o modelo de confiança** do sistema. Até a Fase 3 o
> PDV só mandava dados para fora. Aceitar ordens de fora transforma o terminal
> em alvo: quem comprometer o painel passa a conceder descontos e cancelar
> itens em todas as lojas ao mesmo tempo — e sem pisar em nenhuma delas.

As seis travas, e o que cada uma impede
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
from pdv.domain.models import (
    AuditEventType,
    AuditSeverity,
    Cents,
    EntityId,
    Milligrams,
    iso,
    utc_now,
)
from pdv.remote.inbox import InboxRepository
from pdv.remote.protocol import (
    CommandKind,
    CommandStatus,
    RemoteCommand,
    is_fresh,
    verify_signature,
)
from pdv.services.audit import AuditService

logger = logging.getLogger(__name__)

#: Chave de desligamento do canal remoto. Ausente significa **ligado**: uma loja
#: que nunca abriu a tela de configuração ainda recebe comando do painel. O
#: valor "0" desliga.
REMOTE_ENABLED_KEY = "remote.commands_enabled"

#: Canal gravado na auditoria. O relatório de cancelamentos separa presencial de
#: remoto por este campo. Sem a separação, o painel viraria a rota limpa para o
#: mesmo furto que o módulo anti-fraude existe para combater.
CHANNEL = "remote_panel"


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


class _AlreadySettled(Exception):
    """Outra execução fechou o comando primeiro — a transação inteira volta."""


@dataclass(frozen=True, slots=True)
class ApplyReport:
    applied: int = 0
    refused: int = 0
    deferred: int = 0

    @property
    def total(self) -> int:
        return self.applied + self.refused + self.deferred


class RemoteCommandService:
    """Aplica os comandos pendentes da inbox, um a um."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        checkout: Any | None = None,
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
        """
        self._db = database
        self._config = config
        self._checkout = checkout
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

        applied = refused = deferred = 0
        for command in commands:
            try:
                message = self._apply_one(command)
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

        return ApplyReport(applied=applied, refused=refused, deferred=deferred)

    # -- aplicação ------------------------------------------------------------ #

    def _apply_one(self, command: RemoteCommand) -> str:
        self._check_admissible(command)

        if command.kind is CommandKind.APPLY_DISCOUNT:
            return self._apply_discount(command)
        if command.kind is CommandKind.CANCEL_ITEM:
            return self._cancel_item(command)
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

    def _cancel_item(self, command: RemoteCommand) -> str:
        order_id = _text(command.payload, "order_id")
        item_id = _text(command.payload, "order_item_id")
        reason = _text(command.payload, "reason", "Cancelamento remoto exige motivo.")

        self._require_authorizer(
            command.issued_by_user_id, allowed_roles=frozenset({"manager"})
        )
        self._require_open_order(order_id)

        item = self._db.query_one(
            "SELECT id, product_name, total_cents, canceled_at FROM order_items "
            " WHERE id = ? AND order_id = ?",
            (item_id, order_id),
        )
        if item is None:
            raise CommandRefused("Item não encontrado neste pedido.")
        if item["canceled_at"] is not None:
            raise CommandRefused("O item já estava cancelado.")

        consumptions = self._db.query_all(
            "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients "
            " WHERE order_item_id = ?",
            (item_id,),
        )

        with self._db.transaction() as connection:
            self._claim(connection, command)
            connection.execute(
                "UPDATE order_items SET canceled_at = ?, canceled_by_user_id = ?, "
                "cancel_reason = ? WHERE id = ?",
                (
                    iso(utc_now()),
                    command.issued_by_user_id,
                    f"[{CHANNEL}] {reason}",
                    item_id,
                ),
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
            self._audit(
                connection,
                command,
                event_type=AuditEventType.ITEM_CANCELED,
                severity=AuditSeverity.CRITICAL,
                payload={
                    "order_id": order_id,
                    "order_item_id": item_id,
                    "product_name": str(item["product_name"]),
                    "total_cents": int(item["total_cents"]),
                    "reason": reason,
                },
            )

        live = self._live_sale_for(order_id)
        if live is not None:
            live.items[:] = [i for i in live.items if str(i.id) != item_id]
            live.discount_cents = discount

        return f"item {item['product_name']} cancelado"

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
            "SELECT id, status, subtotal_cents, discount_cents FROM orders "
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
        self, command: RemoteCommand, message: str, severity: AuditSeverity
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
                payload={"kind": command.kind.value, "reason": message},
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
    "REMOTE_ENABLED_KEY",
    "ApplyReport",
    "CommandRefused",
    "RemoteCommandService",
]
