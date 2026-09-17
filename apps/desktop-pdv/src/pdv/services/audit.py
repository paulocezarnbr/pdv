"""Ledger de auditoria anti-furto — imutável e encadeado por hash.

Modelo de ameaça: **o banco local é hostil.** O arquivo SQLite fica na máquina
do caixa, e quem tem acesso físico pode abri-lo com qualquer editor. Portanto a
integridade não pode depender de permissão de arquivo.

Defesa: cada entrada carrega o hash da anterior, formando uma cadeia::

    hash_n = SHA256(prev_hash ‖ seq ‖ event_type ‖ payload_canônico ‖ created_at)

Apagar ou editar uma entrada quebra todos os hashes seguintes. Como a cadeia é
replicada ao servidor no sync, o backend detecta:

* **hash divergente** → conteúdo alterado;
* **buraco no `seq`** → entrada removida;
* **`seq` que nunca chega** → banco truncado.

Não impede a adulteração — **torna-a detectável**, que é o que importa quando a
conversa é com o dono do estabelecimento sobre quem cancelou 40 itens no sábado.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from pdv.data.repositories import OutboxRepository
from pdv.domain.errors import AuditChainError
from pdv.domain.models import (
    GENESIS_HASH,
    AuditEntry,
    AuditEventType,
    AuditSeverity,
    EntityId,
    iso,
    new_id,
    utc_now,
)

def canonical_payload(payload: dict[str, Any]) -> str:
    """JSON canônico: chaves ordenadas e separadores fixos.

    Sem canonicalização, o mesmo conteúdo gera hashes diferentes só por ordem de
    chave — e a verificação da cadeia no servidor falharia sem adulteração
    nenhuma.
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_hash(
    *, prev_hash: str, seq: int, event_type: str, payload_json: str, created_at: str
) -> str:
    material = f"{prev_hash}|{seq}|{event_type}|{payload_json}|{created_at}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditService:
    """Escreve no ledger dentro da transação em andamento.

    Escrever na mesma transação da venda é o ponto central: não existe venda
    sem trilha, nem trilha sem venda.
    """

    def __init__(
        self,
        *,
        tenant_id: EntityId,
        store_id: EntityId,
        device_id: EntityId,
        outbox: OutboxRepository,
    ) -> None:
        self._tenant_id = tenant_id
        self._store_id = store_id
        self._device_id = device_id
        self._outbox = outbox

    # -- escrita -------------------------------------------------------------- #

    def append(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: AuditEventType,
        actor_user_id: EntityId,
        payload: dict[str, Any],
        severity: AuditSeverity = AuditSeverity.INFO,
        authorizer_user_id: EntityId | None = None,
    ) -> AuditEntry:
        """Acrescenta um elo à cadeia. Nunca atualiza nada existente."""
        prev_hash = self._last_hash(connection)
        seq = self._next_seq(connection)
        created_at = iso(utc_now())
        payload_json = canonical_payload(payload)

        digest = compute_hash(
            prev_hash=prev_hash,
            seq=seq,
            event_type=event_type.value,
            payload_json=payload_json,
            created_at=created_at,
        )

        entry_id = new_id()
        client_uuid = new_id()

        connection.execute(
            """
            INSERT INTO audit_ledger
                (id, tenant_id, store_id, device_id, seq, event_type, severity,
                 actor_user_id, authorizer_user_id, payload_json, prev_hash, hash,
                 created_at, client_uuid, is_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                entry_id, self._tenant_id, self._store_id, self._device_id, seq,
                event_type.value, severity.value, actor_user_id, authorizer_user_id,
                payload_json, prev_hash, digest, created_at, client_uuid,
            ),
        )

        self._outbox.enqueue(
            connection,
            entity_table="audit_ledger",
            entity_id=entry_id,
            client_uuid=client_uuid,
            operation="insert",
            payload={
                "id": entry_id,
                "tenant_id": self._tenant_id,
                "store_id": self._store_id,
                "device_id": self._device_id,
                "seq": seq,
                "event_type": event_type.value,
                "severity": severity.value,
                "actor_user_id": actor_user_id,
                "authorizer_user_id": authorizer_user_id,
                "payload_json": payload_json,
                "prev_hash": prev_hash,
                "hash": digest,
                "created_at": created_at,
                "client_uuid": client_uuid,
            },
        )

        return AuditEntry(
            id=EntityId(entry_id),
            seq=seq,
            event_type=event_type,
            severity=severity,
            actor_user_id=actor_user_id,
            authorizer_user_id=authorizer_user_id,
            payload_json=payload_json,
            prev_hash=prev_hash,
            hash=digest,
            created_at=utc_now(),
        )

    # -- verificação ---------------------------------------------------------- #

    def verify_chain(self, connection: sqlite3.Connection) -> None:
        """Revalida a cadeia inteira do terminal.

        Roda na abertura do caixa e antes de cada sync. O servidor repete a
        verificação — nunca confiamos no cliente para atestar a própria
        integridade.

        Raises:
            AuditChainError: hash divergente ou buraco na sequência.
        """
        rows = connection.execute(
            "SELECT seq, event_type, payload_json, prev_hash, hash, created_at "
            "FROM audit_ledger WHERE device_id = ? ORDER BY seq",
            (self._device_id,),
        ).fetchall()

        expected_prev = GENESIS_HASH
        expected_seq = 1

        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                raise AuditChainError(
                    f"Buraco na auditoria: esperado seq {expected_seq}, "
                    f"encontrado {seq}. Entrada removida do banco local."
                )
            if row["prev_hash"] != expected_prev:
                raise AuditChainError(
                    f"Cadeia quebrada no seq {seq}: prev_hash não confere."
                )

            recomputed = compute_hash(
                prev_hash=row["prev_hash"],
                seq=seq,
                event_type=row["event_type"],
                payload_json=row["payload_json"],
                created_at=row["created_at"],
            )
            if recomputed != row["hash"]:
                raise AuditChainError(
                    f"Conteúdo adulterado no seq {seq}: hash não confere."
                )

            expected_prev = row["hash"]
            expected_seq += 1

    # -- internos ------------------------------------------------------------- #

    def _last_hash(self, connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT hash FROM audit_ledger WHERE device_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (self._device_id,),
        ).fetchone()
        return row["hash"] if row else GENESIS_HASH

    def _next_seq(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last FROM audit_ledger WHERE device_id = ?",
            (self._device_id,),
        ).fetchone()
        return int(row["last"]) + 1
