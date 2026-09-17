"""Aplicação idempotente do lote de sincronização — lado servidor.

Este módulo é a metade nuvem da garantia "zero duplicidade, zero perda". O
cliente reenvia sempre que fica em dúvida; cabe aqui reconhecer a repetição.

As quatro regras
----------------

1. **Chave de idempotência é `(tenant_id, client_uuid)`.** Gerada no PDV, viaja
   com o dado e tem índice único. Um `INSERT ... ON CONFLICT DO NOTHING`
   transforma o reenvio em `duplicate` — que o cliente trata como sucesso.

2. **O lote inteiro é uma transação.** Ou entra tudo, ou nada. Aplicação
   parcial deixaria o item de venda gravado sem o movimento de estoque
   correspondente, e o CMV do tenant passaria a mentir em silêncio.

3. **A cadeia de auditoria é revalidada aqui.** O cliente afirma integridade;
   o servidor confere. Nunca se aceita o autoatestado de um banco que fica na
   máquina do caixa.

4. **Marca d'água alta por dispositivo.** Uma vez ancorado o `seq` N, qualquer
   tentativa de reenviar o `seq` N com conteúdo diferente é rejeitada e vira
   alerta de fraude. É isto que torna a venda sincronizada inalcançável para
   quem controla o PC da loja.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class ItemStatus(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SyncItem:
    entity_table: str
    entity_id: str
    client_uuid: str
    operation: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ItemResult:
    client_uuid: str
    status: ItemStatus
    message: str | None = None
    server_seq: int | None = None


#: Tabelas que um terminal pode enviar. Lista fechada: o nome vem do cliente,
#: que é território hostil. Um terminal comprometido não escolhe onde escrever.
WRITABLE_TABLES: frozenset[str] = frozenset(
    {
        "orders",
        "order_items",
        "order_item_ingredients",
        "payments",
        "stock_movements",
        "audit_ledger",
        "cash_sessions",
    }
)


class AuditKeyResolver(Protocol):
    """Recupera o segredo HMAC de um dispositivo (cofre, nunca o banco de app)."""

    def secret_for(self, tenant_id: str, device_id: str) -> bytes: ...


class DeviceAnchorStore(Protocol):
    """Marca d'água alta da cadeia de auditoria, por dispositivo."""

    def last_anchored(self, tenant_id: str, device_id: str) -> tuple[int, str] | None:
        """Retorna `(seq, hash)` do último elo ancorado, ou `None`."""
        ...

    def anchor(self, tenant_id: str, device_id: str, seq: int, digest: str) -> None: ...


class FraudAlertSink(Protocol):
    def raise_alert(self, tenant_id: str, device_id: str, reason: str) -> None: ...


def compute_chain_hash(
    *,
    secret: bytes,
    prev_hash: str,
    seq: int,
    event_type: str,
    payload_json: str,
    created_at: str,
) -> str:
    """Idêntico ao cliente. Divergir aqui invalidaria toda cadeia legítima."""
    material = f"{prev_hash}|{seq}|{event_type}|{payload_json}|{created_at}"
    return hmac.new(secret, material.encode("utf-8"), "sha256").hexdigest()


class SyncMerger:
    """Aplica um lote. Instanciado por request, com a transação já aberta."""

    def __init__(
        self,
        *,
        tenant_id: str,
        store_id: str,
        device_id: str,
        key_resolver: AuditKeyResolver,
        anchors: DeviceAnchorStore,
        alerts: FraudAlertSink,
    ) -> None:
        self._tenant_id = tenant_id
        self._store_id = store_id
        self._device_id = device_id
        self._keys = key_resolver
        self._anchors = anchors
        self._alerts = alerts

    def apply(self, items: list[SyncItem], connection: Any) -> list[ItemResult]:
        """Aplica o lote inteiro. O chamador garante a transação única.

        A ordem importa: entradas de auditoria precisam ser aplicadas na
        sequência em que foram criadas, ou a validação da cadeia falha por
        motivo legítimo.
        """
        results: list[ItemResult] = []

        for item in items:
            if item.entity_table not in WRITABLE_TABLES:
                results.append(
                    ItemResult(
                        item.client_uuid,
                        ItemStatus.REJECTED,
                        f"tabela não gravável por terminal: {item.entity_table}",
                    )
                )
                continue

            if item.entity_table == "audit_ledger":
                results.append(self._apply_audit_entry(item, connection))
            else:
                results.append(self._apply_generic(item, connection))

        return results

    # -- auditoria ------------------------------------------------------------ #

    def _apply_audit_entry(self, item: SyncItem, connection: Any) -> ItemResult:
        payload = item.payload
        seq = int(payload["seq"])
        digest = str(payload["hash"])

        anchored = self._anchors.last_anchored(self._tenant_id, self._device_id)

        # --- Regra 4: marca d'água alta -------------------------------------
        if anchored is not None:
            last_seq, last_hash = anchored

            if seq <= last_seq:
                # Reenvio de algo já ancorado. Se o hash bate, é só uma resposta
                # perdida — duplicata benigna. Se NÃO bate, alguém reescreveu
                # história já registrada na nuvem.
                existing = connection.execute(
                    "SELECT hash FROM audit_ledger WHERE tenant_id = %s "
                    "AND device_id = %s AND seq = %s",
                    (self._tenant_id, self._device_id, seq),
                ).fetchone()

                if existing is not None and existing[0] != digest:
                    self._alerts.raise_alert(
                        self._tenant_id,
                        self._device_id,
                        f"Reescrita de auditoria já ancorada no seq {seq}: "
                        f"servidor tem {existing[0][:16]}…, terminal enviou "
                        f"{digest[:16]}…",
                    )
                    return ItemResult(
                        item.client_uuid,
                        ItemStatus.REJECTED,
                        "seq já ancorado com conteúdo divergente",
                    )
                return ItemResult(item.client_uuid, ItemStatus.DUPLICATE)

            if seq != last_seq + 1:
                # Buraco: o terminal pulou entradas. Ou houve perda, ou alguém
                # apagou o rastro local antes de sincronizar.
                self._alerts.raise_alert(
                    self._tenant_id,
                    self._device_id,
                    f"Buraco na auditoria: esperado seq {last_seq + 1}, "
                    f"recebido {seq}",
                )
                return ItemResult(
                    item.client_uuid,
                    ItemStatus.REJECTED,
                    f"seq fora de ordem (esperado {last_seq + 1})",
                )

            if str(payload["prev_hash"]) != last_hash:
                self._alerts.raise_alert(
                    self._tenant_id,
                    self._device_id,
                    f"Elo quebrado no seq {seq}: prev_hash não corresponde ao "
                    "último elo ancorado",
                )
                return ItemResult(
                    item.client_uuid, ItemStatus.REJECTED, "prev_hash divergente"
                )

        # --- Regra 3: recalcular o HMAC -------------------------------------
        secret = self._keys.secret_for(self._tenant_id, self._device_id)
        expected = compute_chain_hash(
            secret=secret,
            prev_hash=str(payload["prev_hash"]),
            seq=seq,
            event_type=str(payload["event_type"]),
            payload_json=str(payload["payload_json"]),
            created_at=str(payload["created_at"]),
        )

        # Comparação em tempo constante: comparar hash com `==` vaza informação
        # por tempo de resposta e permitiria forjar um elo por tentativa.
        if not hmac.compare_digest(expected, digest):
            self._alerts.raise_alert(
                self._tenant_id,
                self._device_id,
                f"HMAC inválido no seq {seq}: conteúdo adulterado ou chave errada",
            )
            return ItemResult(
                item.client_uuid, ItemStatus.REJECTED, "HMAC da cadeia inválido"
            )

        inserted = self._insert_idempotent(item, connection)
        if inserted:
            self._anchors.anchor(self._tenant_id, self._device_id, seq, digest)
            return ItemResult(item.client_uuid, ItemStatus.APPLIED)
        return ItemResult(item.client_uuid, ItemStatus.DUPLICATE)

    # -- demais entidades ----------------------------------------------------- #

    def _apply_generic(self, item: SyncItem, connection: Any) -> ItemResult:
        # `tenant_id` vem do TOKEN, nunca do payload. Aceitar o do corpo
        # permitiria a um terminal gravar dados no tenant de outro restaurante.
        payload = dict(item.payload)
        payload["tenant_id"] = self._tenant_id
        payload["store_id"] = self._store_id

        inserted = self._insert_idempotent(
            SyncItem(
                item.entity_table,
                item.entity_id,
                item.client_uuid,
                item.operation,
                payload,
            ),
            connection,
        )
        return ItemResult(
            item.client_uuid,
            ItemStatus.APPLIED if inserted else ItemStatus.DUPLICATE,
        )

    def _insert_idempotent(self, item: SyncItem, connection: Any) -> bool:
        """`INSERT ... ON CONFLICT DO NOTHING`. Devolve `True` se gravou agora.

        Regra 1 em uma linha de SQL. O índice único
        `(tenant_id, client_uuid)` é o que dá sentido ao `ON CONFLICT`; sem ele
        o reenvio duplicaria a venda.
        """
        columns = sorted(item.payload.keys())
        column_list = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))

        cursor = connection.execute(
            f"INSERT INTO {item.entity_table} ({column_list}) "  # noqa: S608
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (tenant_id, client_uuid) DO NOTHING "
            f"RETURNING server_seq",
            tuple(item.payload[c] for c in columns),
        )
        row = cursor.fetchone()
        return row is not None
