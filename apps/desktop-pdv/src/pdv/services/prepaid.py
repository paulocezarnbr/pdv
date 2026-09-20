"""Crédito pré-pago offline por ledger append-only."""

from __future__ import annotations

import sqlite3

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    AuditEventType, AuditSeverity, Cents, EntityId, iso, new_id, utc_now,
)
from pdv.services.audit import AuditService


class PrepaidError(PdvError):
    pass


class PrepaidService:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db, self._config = database, config
        self._outbox = OutboxRepository()

    def balance(self, customer_id: EntityId, connection: sqlite3.Connection | None = None) -> Cents:
        db = connection or self._db.connection
        row = db.execute(
            "SELECT coalesce(sum(CASE WHEN entry_type IN ('deposit','refund') "
            "THEN amount_cents ELSE -amount_cents END),0) FROM prepaid_ledger "
            "WHERE tenant_id=? AND customer_id=?",
            (self._config.tenant_id, customer_id),
        ).fetchone()
        return Cents(int(row[0]))

    def deposit(self, *, customer_id: EntityId, amount_cents: Cents,
                actor_user_id: EntityId, authorizer_user_id: EntityId) -> Cents:
        if int(amount_cents) <= 0:
            raise PrepaidError("A carga precisa ser maior que zero.")
        with self._db.transaction() as connection:
            self._append(connection, customer_id=customer_id, entry_type="deposit",
                         amount_cents=amount_cents, order_id=None,
                         actor_user_id=actor_user_id,
                         authorizer_user_id=authorizer_user_id)
            self._audit().append(
                connection, event_type=AuditEventType.PREPAID_CREDITED,
                actor_user_id=actor_user_id, authorizer_user_id=authorizer_user_id,
                severity=AuditSeverity.WARNING,
                payload={"customer_id": customer_id, "amount_cents": int(amount_cents)},
            )
            return self.balance(customer_id, connection)

    def redeem_in(self, connection: sqlite3.Connection, *, customer_id: EntityId,
                  order_id: EntityId, amount_cents: Cents,
                  actor_user_id: EntityId) -> Cents:
        amount = int(amount_cents)
        if amount <= 0:
            raise PrepaidError("Valor pré-pago precisa ser positivo.")
        existing = connection.execute(
            "SELECT amount_cents FROM prepaid_ledger WHERE tenant_id=? "
            "AND order_id=? AND entry_type='debit'",
            (self._config.tenant_id, order_id),
        ).fetchone()
        if existing is not None:
            if int(existing[0]) != amount:
                raise PrepaidError("A venda já consumiu outro valor pré-pago.")
            return self.balance(customer_id, connection)
        if int(self.balance(customer_id, connection)) < amount:
            raise PrepaidError("Saldo pré-pago insuficiente.")
        self._append(connection, customer_id=customer_id, entry_type="debit",
                     amount_cents=amount_cents, order_id=order_id,
                     actor_user_id=actor_user_id, authorizer_user_id=None)
        self._audit().append(
            connection, event_type=AuditEventType.PREPAID_REDEEMED,
            actor_user_id=actor_user_id, severity=AuditSeverity.INFO,
            payload={"customer_id": customer_id, "order_id": order_id,
                     "amount_cents": amount},
        )
        return self.balance(customer_id, connection)

    def _append(self, connection: sqlite3.Connection, *, customer_id: EntityId,
                entry_type: str, amount_cents: Cents, order_id: EntityId | None,
                actor_user_id: EntityId,
                authorizer_user_id: EntityId | None) -> None:
        entry_id, client_uuid, created_at = new_id(), new_id(), iso(utc_now())
        connection.execute(
            "INSERT INTO prepaid_ledger(id,tenant_id,store_id,customer_id,entry_type,"
            "amount_cents,order_id,actor_user_id,authorizer_user_id,created_at,client_uuid) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, self._config.tenant_id, self._config.store_id, customer_id,
             entry_type, int(amount_cents), order_id, actor_user_id,
             authorizer_user_id, created_at, client_uuid),
        )
        self._outbox.enqueue(
            connection, entity_table="prepaid_ledger", entity_id=entry_id,
            client_uuid=client_uuid, operation="insert",
            payload={"id": entry_id, "store_id": self._config.store_id,
                     "customer_id": customer_id, "entry_type": entry_type,
                     "amount_cents": int(amount_cents), "order_id": order_id,
                     "actor_user_id": actor_user_id,
                     "authorizer_user_id": authorizer_user_id,
                     "created_at": created_at},
        )

    def _audit(self) -> AuditService:
        return AuditService(
            tenant_id=self._config.tenant_id, store_id=self._config.store_id,
            device_id=self._config.device_id, outbox=self._outbox,
            device_secret=self._config.device_secret,
        )


__all__ = ["PrepaidError", "PrepaidService"]
