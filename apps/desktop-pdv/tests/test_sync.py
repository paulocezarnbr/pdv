"""Testes da sincronização (Fase 2).

O cenário que estes testes existem para cobrir é o que quebra implementações
ingênuas: **a rede cai depois do servidor gravar e antes da resposta chegar.**
O cliente não tem como saber se o dado entrou. Se ele assumir que falhou,
reenvia e duplica o faturamento. Se assumir que deu certo, perde a venda.

`FakeCloud` reproduz exatamente esse estado com `fail_mode="after_commit"`.
"""

from __future__ import annotations

import hmac
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    Payment,
    PaymentMethod,
    ScaleReading,
    ScaleStatus,
)
from pdv.services.checkout import CheckoutService
from pdv.sync.engine import SyncEngine
from pdv.sync.outbox import MAX_ATTEMPTS, OutboxReader
from pdv.sync.protocol import (
    ItemAck,
    ItemStatus,
    PullResponse,
    PushBatch,
    PushResponse,
    TransportError,
)


# --------------------------------------------------------------------------- #
# Servidor falso — mesma semântica do SyncMerger da cloud-api
# --------------------------------------------------------------------------- #


class FakeCloud:
    """Nuvem em memória com falhas programáveis.

    Implementa as mesmas quatro regras do servidor real: idempotência por
    `(tenant, client_uuid)`, lote atômico, revalidação do HMAC da auditoria e
    marca d'água alta por dispositivo.
    """

    def __init__(self, secret: bytes) -> None:
        self.secret = secret
        self.stored: dict[tuple[str, str], dict] = {}
        #: (device_id, seq) -> hash. Indexado por SEQ, nao por client_uuid:
        #: o ataque de reescrita usa um client_uuid novo para o mesmo seq.
        self.audit_by_seq: dict[tuple[str, int], str] = {}
        self.anchors: dict[str, tuple[int, str]] = {}
        self.alerts: list[str] = []
        self.push_count = 0
        self.received_keys: list[str] = []

        # Programação de falhas
        self.fail_remaining = 0
        self.fail_mode = "before_commit"

    def schedule_failure(self, times: int, mode: str = "before_commit") -> None:
        self.fail_remaining = times
        self.fail_mode = mode

    # -- Transport ------------------------------------------------------------ #

    def push(self, batch: PushBatch) -> PushResponse:
        self.push_count += 1
        self.received_keys.append(batch.idempotency_key)

        failing = self.fail_remaining > 0
        if failing:
            self.fail_remaining -= 1
            if self.fail_mode == "before_commit":
                # Nada foi gravado: o reenvio aplica normalmente.
                raise TransportError("conexão perdida antes do commit")

        acks: list[ItemAck] = []
        for item in batch.items:
            key = (batch.tenant_id, item.client_uuid)

            if item.entity_table == "audit_ledger":
                verdict = self._apply_audit(batch, item, key)
                acks.append(verdict)
                continue

            if key in self.stored:
                acks.append(ItemAck(item.client_uuid, ItemStatus.DUPLICATE))
            else:
                self.stored[key] = {
                    "entity_table": item.entity_table,
                    "payload": item.payload,
                }
                acks.append(ItemAck(item.client_uuid, ItemStatus.APPLIED))

        if failing and self.fail_mode == "after_commit":
            # O CASO PERIGOSO: gravou e a resposta se perdeu. O cliente vai
            # reenviar sem saber, e o servidor precisa responder `duplicate`.
            raise TransportError("conexão perdida após o commit")

        return PushResponse(acks=tuple(acks))

    def pull(self, request) -> PullResponse:  # noqa: ANN001
        return PullResponse(
            entity_table=request.entity_table,
            rows=(),
            last_server_seq=request.since_server_seq,
        )

    # -- regras de auditoria -------------------------------------------------- #

    def _apply_audit(self, batch: PushBatch, item, key) -> ItemAck:  # noqa: ANN001
        payload = item.payload
        seq = int(payload["seq"])
        digest = str(payload["hash"])
        device = batch.device_id

        anchored = self.anchors.get(device)
        if anchored is not None:
            last_seq, last_hash = anchored
            if seq <= last_seq:
                existing = self.audit_by_seq.get((device, seq))
                if existing is not None and existing != digest:
                    self.alerts.append(
                        f"reescrita de auditoria ancorada no seq {seq}"
                    )
                    return ItemAck(
                        item.client_uuid,
                        ItemStatus.REJECTED,
                        "seq já ancorado com conteúdo divergente",
                    )
                return ItemAck(item.client_uuid, ItemStatus.DUPLICATE)

            if seq != last_seq + 1:
                self.alerts.append(f"buraco: esperado {last_seq + 1}, veio {seq}")
                return ItemAck(
                    item.client_uuid, ItemStatus.REJECTED, "seq fora de ordem"
                )

        # Revalidação do HMAC: o servidor nunca confia no autoatestado do cliente.
        material = (
            f'{payload["prev_hash"]}|{seq}|{payload["event_type"]}'
            f'|{payload["payload_json"]}|{payload["created_at"]}'
        )
        expected = hmac.new(self.secret, material.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(expected, digest):
            self.alerts.append(f"HMAC inválido no seq {seq}")
            return ItemAck(item.client_uuid, ItemStatus.REJECTED, "HMAC inválido")

        if key in self.stored:
            return ItemAck(item.client_uuid, ItemStatus.DUPLICATE)

        self.stored[key] = {"entity_table": "audit_ledger", "payload": payload}
        self.audit_by_seq[(device, seq)] = digest
        self.anchors[device] = (seq, digest)
        return ItemAck(item.client_uuid, ItemStatus.APPLIED)

    # -- consultas de teste --------------------------------------------------- #

    def count(self, entity_table: str) -> int:
        return sum(
            1 for row in self.stored.values() if row["entity_table"] == entity_table
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    checkout = CheckoutService(database, config)
    cloud = FakeCloud(config.device_secret)
    engine = SyncEngine(database, cloud, config, batch_size=50)
    return database, config, checkout, cloud, engine


def make_sale(checkout: CheckoutService, database: Database, config: AppConfig,
              grams: int = 892) -> None:
    product = next(
        p
        for p in ProductRepository(database.connection).list_active(
            EntityId(config.tenant_id)
        )
        if p.sku == "TORTA-CHOC"
    )
    checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    checkout.register_weighed_item(
        product=product,
        reading=ScaleReading(ScaleStatus.STABLE, Grams(grams), f"{grams:05d}"),
        operator_id=EntityId(DEMO_OPERATOR_ID),
    )
    total = checkout.current_sale.total_cents
    checkout.finalize_sale(
        payments=(Payment(PaymentMethod.CASH, Cents(int(total))),),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        operator_name="Ana Caixa",
    )


# --------------------------------------------------------------------------- #
# Idempotência
# --------------------------------------------------------------------------- #


def test_idempotency_key_is_stable_for_same_content() -> None:
    """Retentativa do mesmo lote precisa repetir a chave, ou o servidor não
    reconhece a repetição."""
    from pdv.sync.protocol import OutboxItem

    items = tuple(
        OutboxItem(i, "orders", f"e{i}", f"uuid-{i}", "insert", {}) for i in range(3)
    )
    a = PushBatch("dev", "t", "s", items)
    b = PushBatch("dev", "t", "s", items)
    assert a.idempotency_key == b.idempotency_key

    c = PushBatch("dev", "t", "s", items[:2])
    assert c.idempotency_key != a.idempotency_key


def test_duplicate_counts_as_success() -> None:
    assert ItemStatus.DUPLICATE.is_settled
    assert ItemStatus.APPLIED.is_settled
    assert not ItemStatus.REJECTED.is_settled


def test_sale_reaches_the_cloud(env) -> None:  # noqa: ANN001
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)

    pending_before = engine.pending_count()
    assert pending_before > 0

    report = engine.drain()

    assert report.settled == pending_before
    assert engine.pending_count() == 0
    assert cloud.count("orders") == 1


def test_nothing_is_marked_synced_without_ack(env) -> None:  # noqa: ANN001
    """Falha de rede não pode marcar nada como sincronizado."""
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)
    cloud.schedule_failure(times=1, mode="before_commit")

    report = engine.push_once()

    assert report.settled == 0
    assert report.error is not None
    assert engine.pending_count() > 0

    unsynced = database.query_one(
        "SELECT COUNT(*) AS t FROM order_items WHERE is_synced = 0"
    )
    assert int(unsynced["t"]) == 1


def test_lost_response_does_not_duplicate(env) -> None:  # noqa: ANN001
    """O CASO PERIGOSO: servidor gravou, resposta se perdeu.

    O cliente não sabe que deu certo, reenvia, e o servidor responde
    `duplicate`. Resultado correto: **um** registro na nuvem e o item marcado
    como sincronizado no PDV.
    """
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)

    cloud.schedule_failure(times=1, mode="after_commit")
    first = engine.push_once()
    assert first.settled == 0          # o cliente não recebeu confirmação
    assert first.error is not None
    orders_after_first = cloud.count("orders")
    assert orders_after_first == 1     # mas o servidor JÁ gravou

    _force_retry_now(database)
    second = engine.drain()

    assert second.settled > 0
    assert cloud.count("orders") == 1  # NÃO duplicou
    assert engine.pending_count() == 0

    synced = database.query_one(
        "SELECT COUNT(*) AS t FROM order_items WHERE is_synced = 1"
    )
    assert int(synced["t"]) == 1


def test_rejected_item_is_quarantined_not_deleted(env) -> None:  # noqa: ANN001
    """Rejeição tira o item do caminho, mas nunca o apaga."""
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)

    original_push = cloud.push

    def reject_everything(batch: PushBatch) -> PushResponse:
        return PushResponse(
            acks=tuple(
                ItemAck(i.client_uuid, ItemStatus.REJECTED, "payload inválido")
                for i in batch.items
            )
        )

    cloud.push = reject_everything  # type: ignore[method-assign]
    report = engine.push_once()
    cloud.push = original_push  # type: ignore[method-assign]

    assert report.rejected > 0
    assert report.settled == 0
    # Continua no banco, em quarentena — não sumiu.
    assert engine.quarantined_count() == report.rejected
    rows = database.query_all(
        "SELECT attempts, last_error FROM sync_outbox WHERE attempts >= ?",
        (MAX_ATTEMPTS,),
    )
    assert rows and "payload inválido" in rows[0]["last_error"]


def test_unanswered_items_return_to_the_queue(env) -> None:  # noqa: ANN001
    """Silêncio do servidor nunca é interpretado como sucesso."""
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)

    def answer_nothing(batch: PushBatch) -> PushResponse:
        return PushResponse(acks=())

    cloud.push = answer_nothing  # type: ignore[method-assign]
    report = engine.push_once()

    assert report.settled == 0
    assert report.deferred > 0
    assert engine.pending_count() > 0


# --------------------------------------------------------------------------- #
# Detecção de fraude no servidor
# --------------------------------------------------------------------------- #


def test_server_rejects_tampered_audit_entry(env) -> None:  # noqa: ANN001
    """Auditoria adulterada no PDV é recusada pela nuvem."""
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)

    # Atacante edita o payload do ledger no outbox antes do envio.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE sync_outbox SET payload_json = replace(payload_json, "
            "'weight_captured', 'item_registered') "
            "WHERE entity_table = 'audit_ledger'"
        )

    engine.drain()

    assert cloud.alerts, "o servidor deveria ter gerado alerta de fraude"
    assert any("HMAC" in a for a in cloud.alerts)
    assert engine.quarantined_count() > 0


def test_server_rejects_rewrite_of_anchored_seq(env) -> None:  # noqa: ANN001
    """Depois do ACK, a venda está fora do alcance de quem controla o PC."""
    database, config, checkout, cloud, engine = env
    make_sale(checkout, database, config)
    engine.drain()

    anchored_seq, anchored_hash = cloud.anchors[config.device_id]
    assert anchored_seq > 0

    # O atacante tenta reenviar o mesmo seq com outro conteúdo.
    forged = PushBatch(
        device_id=config.device_id,
        tenant_id=config.tenant_id,
        store_id=config.store_id,
        items=(
            _audit_item(
                seq=anchored_seq,
                client_uuid="uuid-forjado",
                digest="f" * 64,
            ),
        ),
    )
    response = cloud.push(forged)

    assert response.acks[0].status is ItemStatus.REJECTED
    assert any("ancorada" in a for a in cloud.alerts)
    # O hash original permanece intacto na nuvem.
    assert cloud.anchors[config.device_id] == (anchored_seq, anchored_hash)


# --------------------------------------------------------------------------- #
# Critério de aceite da Fase 2 (plan.md)
# --------------------------------------------------------------------------- #


def test_acceptance_500_sales_with_3_network_drops(env) -> None:  # noqa: ANN001
    """500 vendas offline, 3 quedas de rede durante o upload.

    Critério do `plan.md`, Fase 2: zero duplicidade e zero perda.
    As quedas usam `after_commit` — o modo que duplica em implementações
    ingênuas.
    """
    database, config, checkout, cloud, engine = env

    SALES = 500
    for i in range(SALES):
        make_sale(checkout, database, config, grams=500 + (i % 400))

    queued = engine.pending_count()
    assert queued > 0

    # Três quedas, todas no pior momento possível.
    cloud.schedule_failure(times=3, mode="after_commit")

    for _ in range(40):
        engine.drain(max_cycles=20)
        if engine.pending_count() == 0:
            break
        _force_retry_now(database)

    # --- Zero perda ---------------------------------------------------------
    assert engine.pending_count() == 0, "sobrou item na fila"
    assert engine.quarantined_count() == 0, "item foi para quarentena"

    local_orders = int(
        database.query_one("SELECT COUNT(*) AS t FROM orders")["t"]
    )
    assert local_orders == SALES
    assert cloud.count("orders") == SALES, "venda perdida no caminho"

    # --- Zero duplicidade ---------------------------------------------------
    # O dicionário do servidor é indexado por (tenant, client_uuid): duplicata
    # seria sobrescrita silenciosa. Conferimos pelo total de itens aceitos.
    applied_uuids = {k[1] for k in cloud.stored}
    assert len(applied_uuids) == len(cloud.stored)

    # --- Tudo marcado como sincronizado no PDV ------------------------------
    unsynced = int(
        database.query_one(
            "SELECT COUNT(*) AS t FROM orders WHERE is_synced = 0"
        )["t"]
    )
    assert unsynced == 0

    unsynced_audit = int(
        database.query_one(
            "SELECT COUNT(*) AS t FROM audit_ledger WHERE is_synced = 0"
        )["t"]
    )
    assert unsynced_audit == 0

    # --- A cadeia de auditoria chegou inteira e em ordem --------------------
    local_max_seq = int(
        database.query_one("SELECT MAX(seq) AS s FROM audit_ledger")["s"]
    )
    assert cloud.anchors[config.device_id][0] == local_max_seq
    assert not cloud.alerts, f"alerta de fraude indevido: {cloud.alerts}"


# --------------------------------------------------------------------------- #
# Auxiliares
# --------------------------------------------------------------------------- #


def _force_retry_now(database: Database) -> None:
    """Zera o backoff para o teste não precisar esperar em tempo real."""
    with database.transaction() as connection:
        connection.execute(
            "UPDATE sync_outbox SET available_at = '2000-01-01T00:00:00.000+00:00'"
        )


def _audit_item(seq: int, client_uuid: str, digest: str):  # noqa: ANN202
    from pdv.sync.protocol import OutboxItem

    return OutboxItem(
        seq=9999,
        entity_table="audit_ledger",
        entity_id="forjado",
        client_uuid=client_uuid,
        operation="insert",
        payload={
            "seq": seq,
            "hash": digest,
            "prev_hash": "0" * 64,
            "event_type": "item_canceled",
            "payload_json": "{}",
            "created_at": "2026-01-01T00:00:00.000+00:00",
        },
    )
