"""Níveis de desconto configuráveis e atribuídos a clientes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import (
    AuditEventType, AuditSeverity, Cents, EntityId, iso, new_id, utc_now,
)
from pdv.services.audit import AuditService


class DiscountTierError(PdvError):
    pass


@dataclass(frozen=True, slots=True)
class DiscountTier:
    id: EntityId
    code: str
    name: str
    percent_basis_points: int
    priority: int
    requires_manager: bool

    def discount_for(self, subtotal: Cents) -> Cents:
        return Cents(int((Decimal(int(subtotal)) * Decimal(self.percent_basis_points)
                          / Decimal(10000)).quantize(Decimal("1"), ROUND_HALF_UP)))


class DiscountTierService:
    CODES = ("bronze", "silver", "gold", "diamond", "employee", "owner")
    PROTECTED_CODES = frozenset({"employee", "owner"})

    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db, self._config = database, config
        self._outbox = OutboxRepository()

    def configure(self, *, code: str, name: str, percent: Decimal, priority: int,
                  requires_manager: bool, actor_user_id: EntityId) -> DiscountTier:
        code = code.strip().lower()
        if code not in self.CODES:
            raise DiscountTierError("Nível desconhecido.")
        if percent < 0 or percent > 100 or not name.strip():
            raise DiscountTierError("Nome ou percentual do nível é inválido.")
        # "Dono" costuma conceder o maior desconto do sistema. A proteção não
        # pode depender de uma checkbox da UI: importação, sync ou código antigo
        # também passam por este serviço. Portanto, senha é invariável do nível.
        if code == "owner":
            requires_manager = True
        basis = int((percent * 100).quantize(Decimal("1"), ROUND_HALF_UP))
        now = iso(utc_now())
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT id,client_uuid FROM discount_tiers WHERE tenant_id=? AND store_id=? AND code=?",
                (self._config.tenant_id, self._config.store_id, code),
            ).fetchone()
            tier_id = EntityId(existing["id"] if existing else new_id())
            client_uuid = EntityId(existing["client_uuid"] if existing else new_id())
            connection.execute(
                "INSERT INTO discount_tiers(id,tenant_id,store_id,code,name,percent_basis_points,"
                "priority,requires_manager,updated_at,client_uuid) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,store_id,code) DO UPDATE SET name=excluded.name,"
                "percent_basis_points=excluded.percent_basis_points,priority=excluded.priority,"
                "requires_manager=excluded.requires_manager,is_active=1,updated_at=excluded.updated_at,is_synced=0",
                (tier_id,self._config.tenant_id,self._config.store_id,code,name.strip(),basis,
                 priority,int(requires_manager),now,client_uuid),
            )
            self._outbox.enqueue(connection,entity_table="discount_tiers",entity_id=tier_id,
                client_uuid=EntityId(new_id()) if existing else client_uuid,
                operation="update" if existing else "insert",
                payload={"id":tier_id,"store_id":self._config.store_id,"code":code,
                         "name":name.strip(),"percent_basis_points":basis,"priority":priority,
                         "requires_manager":requires_manager,"is_active":True,"updated_at":now})
            self._audit().append(connection,event_type=AuditEventType.DISCOUNT_TIER_CONFIGURED,
                actor_user_id=actor_user_id,authorizer_user_id=actor_user_id,
                severity=AuditSeverity.WARNING,
                payload={"tier_id":tier_id,"code":code,"percent_basis_points":basis})
        return DiscountTier(tier_id,code,name.strip(),basis,priority,requires_manager)

    def list_active(self) -> list[DiscountTier]:
        rows = self._db.query_all(
            "SELECT * FROM discount_tiers WHERE tenant_id=? AND store_id=? AND is_active=1 "
            "AND (valid_from IS NULL OR valid_from<=?) AND (valid_until IS NULL OR valid_until>=?) "
            "ORDER BY priority DESC,name",
            (self._config.tenant_id,self._config.store_id,iso(utc_now()),iso(utc_now())),
        )
        return [self._row(row) for row in rows]

    def assign(self, *, customer_id: EntityId, tier_id: EntityId,
               actor_user_id: EntityId) -> None:
        now = iso(utc_now())
        with self._db.transaction() as connection:
            target = connection.execute(
                "SELECT id,code FROM discount_tiers WHERE id=? AND tenant_id=? "
                "AND store_id=? AND is_active=1",
                (tier_id, self._config.tenant_id, self._config.store_id),
            ).fetchone()
            if target is None:
                raise DiscountTierError("Nível não existe ou está inativo.")
            if target["code"] in self.PROTECTED_CODES:
                actor = connection.execute(
                    "SELECT role FROM users WHERE id=? AND tenant_id=? AND is_active=1",
                    (actor_user_id, self._config.tenant_id),
                ).fetchone()
                if actor is None or str(actor["role"]) != "owner":
                    raise DiscountTierError(
                        "Somente um proprietário pode atribuir os níveis "
                        "Funcionário ou Dono."
                    )
            existing = connection.execute(
                "SELECT c.client_uuid,c.tier_id,t.code AS current_code "
                "FROM customer_discount_tiers c "
                "JOIN discount_tiers t ON t.id=c.tier_id "
                "WHERE c.customer_id=? AND c.tenant_id=?",
                (customer_id, self._config.tenant_id),
            ).fetchone()
            if existing and existing["tier_id"] == tier_id:
                return
            if existing and existing["current_code"] in self.PROTECTED_CODES:
                raise DiscountTierError(
                    "Clientes classificados como Funcionário ou Dono não podem "
                    "mudar para outro nível."
                )
            client_uuid = EntityId(existing["client_uuid"] if existing else new_id())
            connection.execute(
                "INSERT INTO customer_discount_tiers(customer_id,tenant_id,tier_id,"
                "assigned_by_user_id,assigned_at,client_uuid) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(customer_id) DO UPDATE SET tier_id=excluded.tier_id,"
                "assigned_by_user_id=excluded.assigned_by_user_id,assigned_at=excluded.assigned_at,is_synced=0",
                (customer_id,self._config.tenant_id,tier_id,actor_user_id,now,client_uuid),
            )
            self._outbox.enqueue(connection,entity_table="customer_discount_tiers",
                entity_id=customer_id,client_uuid=EntityId(new_id()) if existing else client_uuid,
                operation="update" if existing else "insert",
                payload={"customer_id":customer_id,"tier_id":tier_id,
                         "assigned_by_user_id":actor_user_id,"assigned_at":now})
            self._audit().append(connection,event_type=AuditEventType.DISCOUNT_TIER_ASSIGNED,
                actor_user_id=actor_user_id,authorizer_user_id=actor_user_id,
                severity=AuditSeverity.WARNING,
                payload={"customer_id":customer_id,"tier_id":tier_id})

    def for_customer(self, customer_id: EntityId) -> DiscountTier | None:
        now = iso(utc_now())
        row = self._db.query_one(
            "SELECT t.* FROM customer_discount_tiers c JOIN discount_tiers t ON t.id=c.tier_id "
            "WHERE c.tenant_id=? AND c.customer_id=? AND t.is_active=1 "
            "AND (t.valid_from IS NULL OR t.valid_from<=?) "
            "AND (t.valid_until IS NULL OR t.valid_until>=?)",
            (self._config.tenant_id,customer_id,now,now),
        )
        tier = None if row is None else self._row(row)
        # Defesa em profundidade para registros antigos ou adulterados: mesmo
        # que `requires_manager=0` tenha chegado ao banco, Dono exige senha.
        if tier is not None and tier.code == "owner" and not tier.requires_manager:
            return DiscountTier(tier.id, tier.code, tier.name,
                                tier.percent_basis_points, tier.priority, True)
        return tier

    @staticmethod
    def _row(row) -> DiscountTier:  # noqa: ANN001
        return DiscountTier(EntityId(row["id"]),row["code"],row["name"],
            int(row["percent_basis_points"]),int(row["priority"]),bool(row["requires_manager"]))

    def _audit(self) -> AuditService:
        return AuditService(tenant_id=self._config.tenant_id,store_id=self._config.store_id,
            device_id=self._config.device_id,outbox=self._outbox,
            device_secret=self._config.device_secret)


__all__ = ["DiscountTier", "DiscountTierError", "DiscountTierService"]
