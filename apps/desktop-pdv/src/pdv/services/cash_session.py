"""Abertura e conciliação cega do caixa.

O valor esperado nunca sai deste serviço enquanto a sessão está aberta. A UI
recebe apenas identificação, horário e fundo de troco. No fechamento o valor
declarado é persistido primeiro na mesma transação em que o esperado é
calculado; só depois a divergência vira resultado e evento de auditoria.
"""

from __future__ import annotations

from dataclasses import dataclass

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.models import (
    AuditEventType,
    AuditSeverity,
    Cents,
    EntityId,
    iso,
    new_id,
    utc_now,
)
from pdv.services.audit import AuditService


@dataclass(frozen=True, slots=True)
class OpenCashSession:
    id: EntityId
    operator_id: EntityId
    opened_at: str
    opening_cents: Cents


@dataclass(frozen=True, slots=True)
class CashReconciliation:
    session_id: EntityId
    declared_cents: Cents
    expected_cents: Cents
    difference_cents: Cents
    closed_at: str


class CashSessionError(RuntimeError):
    pass


class CashSessionService:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config
        self._outbox = OutboxRepository()

    def current(self) -> OpenCashSession | None:
        row = self._db.connection.execute(
            "SELECT id, user_id, opened_at, opening_amount_cents "
            "FROM cash_sessions WHERE tenant_id = ? AND device_id = ? "
            "AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
            (self._config.tenant_id, self._config.device_id),
        ).fetchone()
        if row is None:
            return None
        return OpenCashSession(
            id=EntityId(row["id"]), operator_id=EntityId(row["user_id"]),
            opened_at=row["opened_at"], opening_cents=Cents(row["opening_amount_cents"]),
        )

    def open(self, *, operator_id: EntityId, opening_cents: Cents) -> OpenCashSession:
        if int(opening_cents) < 0:
            raise CashSessionError("Fundo de troco não pode ser negativo.")
        existing = self.current()
        if existing is not None:
            if existing.operator_id != operator_id:
                raise CashSessionError("Há um caixa aberto por outro operador.")
            return existing
        session = OpenCashSession(new_id(), operator_id, iso(utc_now()), opening_cents)
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO cash_sessions "
                "(id, tenant_id, store_id, device_id, user_id, opened_at, "
                "opening_amount_cents, blind_close, client_uuid, is_synced) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 0)",
                (session.id, self._config.tenant_id, self._config.store_id,
                 self._config.device_id, operator_id, session.opened_at,
                 int(opening_cents), new_id()),
            )
        return session

    def close(
        self,
        *,
        declared_cents: Cents,
        operator_id: EntityId,
        authorizer_id: EntityId | None = None,
    ) -> CashReconciliation:
        if int(declared_cents) < 0:
            raise CashSessionError("Valor contado não pode ser negativo.")
        with self._db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM cash_sessions WHERE tenant_id = ? AND device_id = ? "
                "AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
                (self._config.tenant_id, self._config.device_id),
            ).fetchone()
            if row is None:
                raise CashSessionError("Não há caixa aberto.")
            expected = int(row["opening_amount_cents"]) + int(connection.execute(
                "SELECT coalesce(sum(p.amount_cents - p.change_cents), 0) "
                "FROM payments p JOIN orders o ON o.id = p.order_id "
                "WHERE p.method = 'cash' AND o.device_id = ? "
                "AND p.created_at >= ? AND o.status = 'paid'",
                (self._config.device_id, row["opened_at"]),
            ).fetchone()[0])
            difference = int(declared_cents) - expected
            closed_at = iso(utc_now())
            changed = connection.execute(
                "UPDATE cash_sessions SET closed_at = ?, declared_amount_cents = ?, "
                "expected_amount_cents = ?, difference_cents = ?, is_synced = 0 "
                "WHERE id = ? AND closed_at IS NULL",
                (closed_at, int(declared_cents), expected, difference, row["id"]),
            ).rowcount
            if changed != 1:
                raise CashSessionError("O caixa já foi fechado.")
            self._audit().append(
                connection, event_type=AuditEventType.SESSION_CLOSED,
                actor_user_id=operator_id, authorizer_user_id=authorizer_id,
                severity=AuditSeverity.WARNING if difference else AuditSeverity.INFO,
                payload={"cash_session_id": row["id"], "declared_cents": int(declared_cents),
                         "expected_cents": expected, "difference_cents": difference,
                         "blind_close": True},
            )
            self._outbox.enqueue(
                connection, entity_table="cash_sessions", entity_id=EntityId(row["id"]),
                client_uuid=EntityId(row["client_uuid"]), operation="insert",
                payload={"id": row["id"], "store_id": self._config.store_id,
                         "device_id": self._config.device_id, "operator_id": row["user_id"],
                         "opened_at": row["opened_at"], "closed_at": closed_at,
                         "opening_cents": int(row["opening_amount_cents"]),
                         "declared_cents": int(declared_cents), "expected_cents": expected,
                         "difference_cents": difference, "blind_close": True,
                         "client_uuid": row["client_uuid"]},
            )
        return CashReconciliation(EntityId(row["id"]), declared_cents, Cents(expected),
                                  Cents(difference), closed_at)

    def _audit(self) -> AuditService:
        return AuditService(tenant_id=self._config.tenant_id, store_id=self._config.store_id,
                            device_id=self._config.device_id, outbox=self._outbox,
                            device_secret=self._config.device_secret)


__all__ = ["CashReconciliation", "CashSessionError", "CashSessionService", "OpenCashSession"]
