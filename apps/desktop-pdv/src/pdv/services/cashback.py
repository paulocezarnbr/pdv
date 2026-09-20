"""Cashback offline com créditos imutáveis e consumo FIFO por lote."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.models import (
    AuditEventType, AuditSeverity, Cents, EntityId, iso, new_id, utc_now,
)
from pdv.services.audit import AuditService


class CashbackError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CashbackRule:
    percent_basis_points: int
    max_per_sale_cents: Cents
    validity_days: int


@dataclass(frozen=True, slots=True)
class CashbackCredit:
    id: EntityId
    amount_cents: Cents
    expires_at: str


@dataclass(frozen=True, slots=True)
class Customer:
    id: EntityId
    name: str
    phone: str | None


class CashbackService:
    """Configura, credita e resgata sem jamais atualizar um saldo."""

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config
        self._outbox = OutboxRepository()

    def configure(self, *, percent: Decimal, max_per_sale_cents: Cents,
                  validity_days: int, actor_user_id: EntityId | None = None) -> CashbackRule:
        if percent < 0 or percent > 100:
            raise CashbackError("Percentual precisa estar entre 0 e 100.")
        if int(max_per_sale_cents) < 0 or not 1 <= validity_days <= 3650:
            raise CashbackError("Teto e validade do cashback são inválidos.")
        basis_points = int((percent * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO cashback_rules "
                "(id,tenant_id,store_id,percent_basis_points,max_per_sale_cents,"
                "validity_days,is_active,updated_at) VALUES(?,?,?,?,?,?,1,?) "
                "ON CONFLICT(tenant_id,store_id) DO UPDATE SET "
                "percent_basis_points=excluded.percent_basis_points, "
                "max_per_sale_cents=excluded.max_per_sale_cents, "
                "validity_days=excluded.validity_days,is_active=1,updated_at=excluded.updated_at",
                (new_id(), self._config.tenant_id, self._config.store_id, basis_points,
                 int(max_per_sale_cents), validity_days, now),
            )
            if actor_user_id is not None:
                AuditService(
                    tenant_id=self._config.tenant_id, store_id=self._config.store_id,
                    device_id=self._config.device_id, outbox=self._outbox,
                    device_secret=self._config.device_secret,
                ).append(
                    connection, event_type=AuditEventType.CASHBACK_RULE_CHANGED,
                    actor_user_id=actor_user_id, authorizer_user_id=actor_user_id,
                    severity=AuditSeverity.WARNING,
                    payload={"operation": "cashback_rule_changed",
                             "percent_basis_points": basis_points,
                             "max_per_sale_cents": int(max_per_sale_cents),
                             "validity_days": validity_days},
                )
        return CashbackRule(basis_points, max_per_sale_cents, validity_days)

    def create_customer(self, *, name: str, phone: str | None = None) -> EntityId:
        if not name.strip():
            raise CashbackError("Nome do cliente é obrigatório.")
        normalized_phone = (
            "".join(character for character in phone if character.isdigit())
            if phone else None
        )
        customer_id, client_uuid, now = new_id(), new_id(), iso(utc_now())
        with self._db.transaction() as connection:
            connection.execute(
                "INSERT INTO customers(id,tenant_id,name,phone,created_at,updated_at,client_uuid) "
                "VALUES(?,?,?,?,?,?,?)",
                (customer_id, self._config.tenant_id, name.strip(),
                 normalized_phone, now, now, client_uuid),
            )
            self._outbox.enqueue(connection, entity_table="customers", entity_id=customer_id,
                                 client_uuid=client_uuid, operation="insert",
                                 payload={"id": customer_id, "name": name.strip(),
                                          "phone": normalized_phone,
                                          "created_at": now, "updated_at": now})
        return customer_id

    def find_customer_by_phone(self, phone: str) -> Customer | None:
        normalized = "".join(character for character in phone if character.isdigit())
        if not normalized:
            return None
        row = self._db.query_one(
            "SELECT id,name,phone FROM customers WHERE tenant_id=? AND phone=? AND is_active=1",
            (self._config.tenant_id, normalized),
        )
        return None if row is None else Customer(EntityId(row["id"]), row["name"], row["phone"])

    def earn(self, *, customer_id: EntityId, order_id: EntityId,
             eligible_cents: Cents, actor_user_id: EntityId) -> CashbackCredit | None:
        """Credita uma venda uma única vez; repetição do pedido é idempotente."""
        with self._db.transaction() as connection:
            return self.earn_in(
                connection, customer_id=customer_id, order_id=order_id,
                eligible_cents=eligible_cents, actor_user_id=actor_user_id,
            )

    def earn_in(self, connection: sqlite3.Connection, *, customer_id: EntityId,
                order_id: EntityId, eligible_cents: Cents,
                actor_user_id: EntityId) -> CashbackCredit | None:
        """Versão componível para entrar na mesma transação do fechamento."""
        existing = connection.execute(
                "SELECT id,amount_cents,expires_at FROM cashback_ledger "
                "WHERE tenant_id=? AND order_id=? AND entry_type='credit'",
                (self._config.tenant_id, order_id),
            ).fetchone()
        if existing:
            return CashbackCredit(EntityId(existing["id"]), Cents(existing["amount_cents"]),
                                  str(existing["expires_at"]))
        rule = connection.execute(
                "SELECT * FROM cashback_rules WHERE tenant_id=? AND store_id=? AND is_active=1",
                (self._config.tenant_id, self._config.store_id),
            ).fetchone()
        if rule is None or int(eligible_cents) <= 0:
            return None
        amount = int((Decimal(int(eligible_cents)) * Decimal(int(rule["percent_basis_points"]))
                      / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        cap = int(rule["max_per_sale_cents"])
        if cap:
            amount = min(amount, cap)
        if amount <= 0:
            return None
        credit_id, client_uuid = new_id(), new_id()
        created = utc_now()
        expires = iso(created + timedelta(days=int(rule["validity_days"])))
        connection.execute(
                "INSERT INTO cashback_ledger "
                "(id,tenant_id,store_id,customer_id,order_id,entry_type,amount_cents,"
                "expires_at,created_at,actor_user_id,client_uuid) VALUES(?,?,?,?,?,'credit',?,?,?,?,?)",
                (credit_id, self._config.tenant_id, self._config.store_id, customer_id,
                 order_id, amount, expires, iso(created), actor_user_id, client_uuid),
            )
        self._enqueue_ledger(connection, credit_id, client_uuid, customer_id, order_id,
                             "credit", amount, None, expires, actor_user_id)
        return CashbackCredit(credit_id, Cents(amount), expires)

    def balance(self, customer_id: EntityId) -> Cents:
        now = iso(utc_now())
        row = self._db.connection.execute(
            "SELECT coalesce(sum(c.amount_cents - coalesce((SELECT sum(d.amount_cents) "
            "FROM cashback_ledger d WHERE d.source_credit_id=c.id AND d.entry_type='debit'),0)),0) "
            "FROM cashback_ledger c WHERE c.tenant_id=? AND c.customer_id=? "
            "AND c.entry_type='credit' AND c.expires_at>?",
            (self._config.tenant_id, customer_id, now),
        ).fetchone()
        return Cents(int(row[0]))

    def redeem(self, *, customer_id: EntityId, order_id: EntityId, amount_cents: Cents,
               actor_user_id: EntityId) -> Cents:
        """Consome créditos não vencidos em FIFO, criando um débito por lote."""
        wanted = int(amount_cents)
        if wanted <= 0:
            raise CashbackError("Valor de resgate precisa ser positivo.")
        now = iso(utc_now())
        with self._db.transaction() as connection:
            credits = connection.execute(
                "SELECT c.id,c.amount_cents,c.expires_at, "
                "c.amount_cents-coalesce((SELECT sum(d.amount_cents) FROM cashback_ledger d "
                "WHERE d.source_credit_id=c.id AND d.entry_type='debit'),0) available "
                "FROM cashback_ledger c WHERE c.tenant_id=? AND c.customer_id=? "
                "AND c.entry_type='credit' AND c.expires_at>? ORDER BY c.expires_at,c.created_at",
                (self._config.tenant_id, customer_id, now),
            ).fetchall()
            if sum(max(0, int(row["available"])) for row in credits) < wanted:
                raise CashbackError("Saldo de cashback insuficiente.")
            remaining = wanted
            for credit in credits:
                take = min(remaining, max(0, int(credit["available"])))
                if not take:
                    continue
                debit_id, client_uuid = new_id(), new_id()
                connection.execute(
                    "INSERT INTO cashback_ledger "
                    "(id,tenant_id,store_id,customer_id,order_id,entry_type,amount_cents,"
                    "source_credit_id,created_at,actor_user_id,client_uuid) "
                    "VALUES(?,?,?,?,?,'debit',?,?,?,?,?)",
                    (debit_id, self._config.tenant_id, self._config.store_id, customer_id,
                     order_id, take, credit["id"], now, actor_user_id, client_uuid),
                )
                self._enqueue_ledger(connection, debit_id, client_uuid, customer_id, order_id,
                                     "debit", take, EntityId(credit["id"]), None, actor_user_id)
                remaining -= take
                if remaining == 0:
                    break
        return amount_cents

    def _enqueue_ledger(self, connection: sqlite3.Connection, entry_id: EntityId,
                        client_uuid: EntityId, customer_id: EntityId, order_id: EntityId,
                        entry_type: str, amount: int, source_credit_id: EntityId | None,
                        expires_at: str | None, actor_user_id: EntityId) -> None:
        self._outbox.enqueue(
            connection, entity_table="cashback_ledger", entity_id=entry_id,
            client_uuid=client_uuid, operation="insert",
            payload={"id": entry_id, "store_id": self._config.store_id,
                     "customer_id": customer_id, "order_id": order_id,
                     "entry_type": entry_type, "amount_cents": amount,
                     "source_credit_id": source_credit_id, "expires_at": expires_at,
                     "actor_user_id": actor_user_id},
        )


__all__ = ["CashbackCredit", "CashbackError", "CashbackRule", "CashbackService", "Customer"]
