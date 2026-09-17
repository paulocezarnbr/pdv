"""Ledger de auditoria anti-furto — encadeado por HMAC.

Modelo de ameaça: **o banco local é hostil.** O arquivo SQLite fica na máquina
do caixa e quem tem acesso físico pode abri-lo com qualquer editor de SQLite.
A integridade não pode depender de permissão de arquivo nem do gatilho do banco.

Por que HMAC e não SHA-256 puro
--------------------------------

Um encadeamento com hash público (``SHA256(prev ‖ dados)``) **não protege nada**
quando o atacante tem o código, e num app Python ele sempre tem. Basta editar a
linha e recalcular a cadeia inteira a partir dali — o algoritmo está publicado
no próprio repositório. A cadeia continuaria "válida".

Com HMAC o elo depende de um segredo que não está no banco::

    hash_n = HMAC-SHA256(device_secret, prev_hash ‖ seq ‖ event_type
                                        ‖ payload_canônico ‖ created_at)

Sem a chave, forjar um elo é computacionalmente inviável. Isso **eleva a
barreira** de "qualquer um com o DB Browser" para "quem consegue extrair a
chave do DPAPI do Windows". Não é inviolável — ver abaixo.

O que realmente garante a integridade
--------------------------------------

Nenhuma criptografia local protege dados em hardware controlado pelo atacante:
com privilégio de administrador e um depurador, a chave sai da memória. A
garantia forte vem de **fora da máquina**:

1. **Ancoragem no servidor.** Toda entrada sincronizada tem cópia na nuvem. A
   partir do ACK, adulterar a versão local não muda nada: a verdade já saiu.
2. **Marca d'água alta (high-water mark).** O servidor guarda o último `seq`
   por dispositivo. Reenviar um `seq` já ancorado com conteúdo diferente é
   rejeitado e vira alerta de fraude.
3. **Sequência contígua.** `seq` sem buraco por dispositivo e `local_number`
   sem buraco por terminal: apagar uma venda deixa um vão que o servidor vê.
4. **Sincronização frequente.** A janela de vulnerabilidade é exatamente o
   intervalo entre a venda e o ACK. Por isso o worker sobe em segundos, não em
   horas — é uma decisão de *segurança*, não de performance.

Em resumo: a adulteração local não é impedida, é **detectada** — e a janela em
que ela vale alguma coisa é medida em segundos.
"""

from __future__ import annotations

import hmac
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
    *,
    secret: bytes,
    prev_hash: str,
    seq: int,
    event_type: str,
    payload_json: str,
    created_at: str,
) -> str:
    """Elo da cadeia: HMAC-SHA256 com o segredo do dispositivo.

    O `|` como separador não é decorativo: sem delimitador, dois campos
    diferentes poderiam concatenar no mesmo material (``"ab"+"c"`` vs
    ``"a"+"bc"``) e produzir elos colidentes.
    """
    material = f"{prev_hash}|{seq}|{event_type}|{payload_json}|{created_at}"
    return hmac.new(secret, material.encode("utf-8"), "sha256").hexdigest()


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
        device_secret: bytes,
    ) -> None:
        if not device_secret:
            raise ValueError("device_secret vazio: a cadeia seria forjavel")
        self._tenant_id = tenant_id
        self._store_id = store_id
        self._device_id = device_id
        self._outbox = outbox
        self._secret = device_secret

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
            secret=self._secret,
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
                secret=self._secret,
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
