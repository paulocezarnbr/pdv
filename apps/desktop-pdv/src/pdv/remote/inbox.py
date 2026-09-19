"""Fila de entrada de comandos remotos — o espelho do outbox.

A assimetria com o outbox é proposital e vale explicar, porque as duas filas
parecem iguais e falham de jeitos opostos:

* **No outbox, a dúvida manda reenviar.** Perder uma venda é irreversível;
  reenviar é seguro porque o servidor deduplica por `client_uuid`.
* **Aqui, a dúvida manda NÃO aplicar.** Aplicar um desconto duas vezes é
  perda de dinheiro que ninguém reclama — o cliente não avisa que pagou menos.
  Por isso o status sai de `pending` na mesma transação em que o efeito
  acontece: se o processo morrer no meio, a transação inteira volta atrás e o
  comando continua pendente; se o processo morrer depois, o status já mudou e
  a reentrega não reaplica.

`reported_at` é separado de `settled_at` pelo mesmo motivo: avisar a nuvem é
uma operação de rede e pode falhar. Falhar ao avisar não pode desfazer o que
já foi aplicado, nem autorizar uma segunda aplicação.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from pdv.data.database import Database
from pdv.domain.models import iso, utc_now
from pdv.remote.protocol import CommandKind, CommandStatus, RemoteCommand

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """O que o terminal responde à nuvem sobre um comando."""

    command_uuid: str
    status: CommandStatus
    message: str
    settled_at: str


class InboxRepository:
    """Persistência da fila de comandos."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- entrada -------------------------------------------------------------- #

    def accept(self, command: RemoteCommand) -> bool:
        """Grava um comando recebido. Devolve se ele é novo.

        A colisão de `command_uuid` **não** é erro: é a reentrega funcionando.
        O `INSERT OR IGNORE` transforma o caso normal de uma rede instável em
        um no-op silencioso, que é o comportamento certo.
        """
        with self._db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO remote_commands
                    (command_uuid, tenant_id, store_id, device_id, kind,
                     payload_json, issued_by_user_id, issued_by_name, issued_at,
                     signature, status, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    command.command_uuid,
                    command.tenant_id,
                    command.store_id,
                    command.device_id,
                    command.kind.value,
                    json.dumps(command.payload, sort_keys=True, ensure_ascii=False),
                    command.issued_by_user_id,
                    command.issued_by_name,
                    command.issued_at,
                    command.signature,
                    iso(utc_now()),
                ),
            )
            return cursor.rowcount > 0

    # -- leitura -------------------------------------------------------------- #

    def pending(self, limit: int = 50) -> list[RemoteCommand]:
        """Comandos ainda não aplicados, na ordem em que chegaram."""
        rows = self._db.query_all(
            "SELECT * FROM remote_commands WHERE status = 'pending' "
            " ORDER BY received_at, rowid LIMIT ?",
            (limit,),
        )
        return [_to_command(row) for row in rows]

    def pending_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM remote_commands WHERE status = 'pending'"
        )
        return int(row["n"]) if row else 0

    def status_of(self, command_uuid: str) -> CommandStatus | None:
        row = self._db.query_one(
            "SELECT status FROM remote_commands WHERE command_uuid = ?",
            (command_uuid,),
        )
        return CommandStatus(str(row["status"])) if row else None

    def unreported(self, limit: int = 50) -> list[CommandResult]:
        """Resultados já decididos que a nuvem ainda não confirmou."""
        rows = self._db.query_all(
            "SELECT command_uuid, status, result_message, settled_at "
            "  FROM remote_commands "
            " WHERE status <> 'pending' AND reported_at IS NULL "
            " ORDER BY settled_at LIMIT ?",
            (limit,),
        )
        return [
            CommandResult(
                command_uuid=str(row["command_uuid"]),
                status=CommandStatus(str(row["status"])),
                message=str(row["result_message"] or ""),
                settled_at=str(row["settled_at"] or ""),
            )
            for row in rows
        ]

    # -- saída ---------------------------------------------------------------- #

    def settle_in(
        self,
        connection: sqlite3.Connection,
        command_uuid: str,
        status: CommandStatus,
        message: str,
    ) -> bool:
        """Fecha o comando **dentro da transação que aplicou o efeito**.

        Receber a conexão em vez de abrir a própria é o ponto inteiro deste
        método: o desconto e a mudança de status entram juntos ou não entram.
        A cláusula `status = 'pending'` é a trava final contra aplicação dupla —
        se duas execuções chegarem aqui, a segunda atualiza zero linhas.
        """
        cursor = connection.execute(
            "UPDATE remote_commands "
            "   SET status = ?, result_message = ?, settled_at = ? "
            " WHERE command_uuid = ? AND status = 'pending'",
            (status.value, message, iso(utc_now()), command_uuid),
        )
        return cursor.rowcount > 0

    def settle(
        self, command_uuid: str, status: CommandStatus, message: str
    ) -> bool:
        """Fecha um comando que não teve efeito no banco (recusa)."""
        with self._db.transaction() as connection:
            return self.settle_in(connection, command_uuid, status, message)

    def mark_reported(self, command_uuids: list[str]) -> None:
        """Marca os resultados que a nuvem confirmou ter recebido."""
        if not command_uuids:
            return
        now = iso(utc_now())
        with self._db.transaction() as connection:
            connection.executemany(
                "UPDATE remote_commands SET reported_at = ? "
                " WHERE command_uuid = ? AND reported_at IS NULL",
                [(now, uuid) for uuid in command_uuids],
            )


def _to_command(row: sqlite3.Row) -> RemoteCommand:
    payload: dict[str, Any] = json.loads(str(row["payload_json"]))
    return RemoteCommand(
        command_uuid=str(row["command_uuid"]),
        tenant_id=str(row["tenant_id"]),
        store_id=str(row["store_id"]),
        device_id=str(row["device_id"]),
        kind=CommandKind(str(row["kind"])),
        payload=payload,
        issued_by_user_id=str(row["issued_by_user_id"]),
        issued_by_name=str(row["issued_by_name"]),
        issued_at=str(row["issued_at"]),
        signature=str(row["signature"]),
    )


__all__ = ["CommandResult", "InboxRepository"]
