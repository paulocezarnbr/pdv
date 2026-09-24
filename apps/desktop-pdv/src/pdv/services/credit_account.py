"""Fiado/Pendura com limite automático e aging derivado do ledger."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    AuditEventType, AuditSeverity, Cents, EntityId, iso, new_id, utc_now,
)
from pdv.services.audit import AuditService


class CreditAccountError(PdvError):
    pass


@dataclass(frozen=True, slots=True)
class CreditPosition:
    limit_cents: Cents
    outstanding_cents: Cents
    available_cents: Cents
    overdue_cents: Cents


class CreditAccountService:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db, self._config = database, config
        self._outbox = OutboxRepository()

    def configure(self, *, customer_id: EntityId, limit_cents: Cents, due_days: int,
                  actor_user_id: EntityId, authorizer_user_id: EntityId) -> CreditPosition:
        if int(limit_cents) < 0 or not 1 <= due_days <= 365:
            raise CreditAccountError("Limite ou prazo do fiado é inválido.")
        now, client_uuid = iso(utc_now()), new_id()
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT client_uuid FROM customer_credit_accounts WHERE customer_id=?",
                (customer_id,),
            ).fetchone()
            if existing is not None:
                client_uuid = EntityId(existing["client_uuid"])
            connection.execute(
                "INSERT INTO customer_credit_accounts(customer_id,tenant_id,limit_cents,"
                "due_days,is_active,updated_at,client_uuid) VALUES(?,?,?,?,1,?,?) "
                "ON CONFLICT(customer_id) DO UPDATE SET limit_cents=excluded.limit_cents,"
                "due_days=excluded.due_days,is_active=1,updated_at=excluded.updated_at,is_synced=0",
                (customer_id, self._config.tenant_id, int(limit_cents), due_days, now, client_uuid),
            )
            self._outbox.enqueue(
                connection, entity_table="customer_credit_accounts",
                entity_id=customer_id,
                # Atualização leva identidade própria: com o `client_uuid` da
                # linha, a nuvem a descartava como duplicata do cadastro.
                client_uuid=EntityId(new_id()) if existing else client_uuid,
                operation="update" if existing else "insert",
                payload={"customer_id": customer_id, "limit_cents": int(limit_cents),
                         "due_days": due_days, "is_active": True, "updated_at": now},
            )
            self._audit().append(
                connection, event_type=AuditEventType.CREDIT_ACCOUNT_CONFIGURED,
                actor_user_id=actor_user_id, authorizer_user_id=authorizer_user_id,
                severity=AuditSeverity.WARNING,
                payload={"customer_id": customer_id, "limit_cents": int(limit_cents),
                         "due_days": due_days},
            )
        return self.position(customer_id)

    def position(self, customer_id: EntityId,
                 connection: sqlite3.Connection | None = None) -> CreditPosition:
        db = connection or self._db.connection
        account = db.execute(
            "SELECT limit_cents FROM customer_credit_accounts WHERE tenant_id=? "
            "AND customer_id=? AND is_active=1",
            (self._config.tenant_id, customer_id),
        ).fetchone()
        limit = int(account[0]) if account else 0
        now = iso(utc_now())
        charges = db.execute(
            "SELECT c.id,c.amount_cents,c.due_at,coalesce((SELECT sum(p.amount_cents) "
            "FROM credit_account_ledger p WHERE p.source_charge_id=c.id "
            "AND p.entry_type IN ('payment','forgive')),0) settled "
            "FROM credit_account_ledger c WHERE c.tenant_id=? AND c.customer_id=? "
            "AND c.entry_type='charge'",
            (self._config.tenant_id, customer_id),
        ).fetchall()
        outstanding = sum(max(0, int(row["amount_cents"])-int(row["settled"])) for row in charges)
        overdue = sum(
            max(0, int(row["amount_cents"])-int(row["settled"]))
            for row in charges if row["due_at"] and str(row["due_at"]) < now
        )
        return CreditPosition(Cents(limit), Cents(outstanding),
                              Cents(max(0, limit-outstanding)), Cents(overdue))

    def charge_in(self, connection: sqlite3.Connection, *, customer_id: EntityId,
                  order_id: EntityId, amount_cents: Cents,
                  actor_user_id: EntityId) -> CreditPosition:
        amount = int(amount_cents)
        if amount <= 0:
            raise CreditAccountError("Valor do fiado precisa ser positivo.")
        existing = connection.execute(
            "SELECT amount_cents FROM credit_account_ledger WHERE tenant_id=? "
            "AND order_id=? AND entry_type='charge'",
            (self._config.tenant_id, order_id),
        ).fetchone()
        if existing:
            if int(existing[0]) != amount:
                raise CreditAccountError("A venda já possui outra cobrança no fiado.")
            return self.position(customer_id, connection)
        account = connection.execute(
            "SELECT due_days FROM customer_credit_accounts WHERE tenant_id=? "
            "AND customer_id=? AND is_active=1",
            (self._config.tenant_id, customer_id),
        ).fetchone()
        position = self.position(customer_id, connection)
        if account is None or amount > int(position.available_cents):
            raise CreditAccountError("Limite disponível do fiado é insuficiente.")
        due_at = iso(utc_now() + timedelta(days=int(account["due_days"])))
        self._append(connection, customer_id, "charge", amount_cents, order_id,
                     None, due_at, actor_user_id, None)
        self._audit().append(
            connection, event_type=AuditEventType.CREDIT_ACCOUNT_CHARGED,
            actor_user_id=actor_user_id, severity=AuditSeverity.WARNING,
            payload={"customer_id": customer_id, "order_id": order_id,
                     "amount_cents": amount, "due_at": due_at},
        )
        return self.position(customer_id, connection)

    def pay(self, *, customer_id: EntityId, amount_cents: Cents,
            actor_user_id: EntityId) -> CreditPosition:
        remaining = int(amount_cents)
        if remaining <= 0:
            raise CreditAccountError("Pagamento precisa ser positivo.")
        with self._db.transaction() as connection:
            charges = connection.execute(
                "SELECT c.id,c.amount_cents,coalesce((SELECT sum(p.amount_cents) "
                "FROM credit_account_ledger p WHERE p.source_charge_id=c.id AND "
                "p.entry_type IN ('payment','forgive')),0) settled FROM credit_account_ledger c "
                "WHERE c.tenant_id=? AND c.customer_id=? AND c.entry_type='charge' "
                "ORDER BY c.due_at,c.created_at",
                (self._config.tenant_id, customer_id),
            ).fetchall()
            outstanding = sum(max(0, int(r["amount_cents"])-int(r["settled"])) for r in charges)
            if remaining > outstanding:
                raise CreditAccountError("Pagamento supera a dívida em aberto.")
            for charge in charges:
                take = min(remaining, max(0, int(charge["amount_cents"])-int(charge["settled"])))
                if take:
                    self._append(connection, customer_id, "payment", Cents(take), None,
                                 EntityId(charge["id"]), None, actor_user_id, None)
                    remaining -= take
                if remaining == 0:
                    break
            self._audit().append(
                connection, event_type=AuditEventType.CREDIT_ACCOUNT_PAID,
                actor_user_id=actor_user_id, severity=AuditSeverity.INFO,
                payload={"customer_id": customer_id, "amount_cents": int(amount_cents)},
            )
            return self.position(customer_id, connection)

    def _append(self, connection: sqlite3.Connection, customer_id: EntityId,
                entry_type: str, amount: Cents, order_id: EntityId | None,
                source: EntityId | None, due_at: str | None, actor: EntityId,
                authorizer: EntityId | None) -> None:
        entry_id, client_uuid, created = new_id(), new_id(), iso(utc_now())
        connection.execute(
            "INSERT INTO credit_account_ledger(id,tenant_id,store_id,customer_id,entry_type,"
            "amount_cents,order_id,source_charge_id,due_at,actor_user_id,authorizer_user_id,"
            "created_at,client_uuid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id,self._config.tenant_id,self._config.store_id,customer_id,entry_type,
             int(amount),order_id,source,due_at,actor,authorizer,created,client_uuid),
        )
        self._outbox.enqueue(connection, entity_table="credit_account_ledger",
            entity_id=entry_id, client_uuid=client_uuid, operation="insert",
            payload={"id":entry_id,"store_id":self._config.store_id,"customer_id":customer_id,
                     "entry_type":entry_type,"amount_cents":int(amount),"order_id":order_id,
                     "source_charge_id":source,"due_at":due_at,"actor_user_id":actor,
                     "authorizer_user_id":authorizer,"created_at":created})

    def _audit(self) -> AuditService:
        return AuditService(tenant_id=self._config.tenant_id,store_id=self._config.store_id,
            device_id=self._config.device_id,outbox=self._outbox,
            device_secret=self._config.device_secret)


__all__ = ["CreditAccountError", "CreditAccountService", "CreditPosition"]
