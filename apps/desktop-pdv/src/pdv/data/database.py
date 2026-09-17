"""Conexão SQLite local e controle transacional.

Escolhas que não são negociáveis num PDV:

* **WAL** — leitura concorrente durante escrita. Sem ele, o worker de
  sincronização travaria a tela do caixa a cada lote enviado.
* **synchronous=FULL** — sim, é mais lento. Também é o que garante que a venda
  já confirmada não some numa queda de energia. Estabelecimento de food service
  perde energia; é o custo de estar certo.
* **foreign_keys=ON** — o SQLite desliga FK por padrão. Sem isso, um item
  órfão de venda entra calado no banco e só aparece no relatório do contador.
* **busy_timeout** — a UI e o sync disputam o arquivo; esperar é melhor que
  estourar `database is locked` na frente do cliente.

Não usamos ORM aqui de propósito: o schema offline é pequeno e o controle
transacional precisa ser explícito (a venda inteira é uma transação só).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final[int] = 1
_SCHEMA_FILE: Final[Path] = Path(__file__).with_name("schema.sql")


class Database:
    """Dona da conexão. Uma instância por processo."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None

    # -- ciclo de vida -------------------------------------------------------- #

    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            timeout=30.0,
            isolation_level=None,  # controlamos BEGIN/COMMIT explicitamente
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row

        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA temp_store = MEMORY")

        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    # -- migrations ----------------------------------------------------------- #

    def migrate(self) -> None:
        """Aplica o schema de forma idempotente, versionado por `user_version`.

        Migrations futuras entram como blocos numerados aqui — nunca editando
        `schema.sql` em cima de uma base já instalada em loja.
        """
        connection = self.connect()
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])

        if current < 1:
            connection.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- transação ------------------------------------------------------------ #

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transação atômica — tudo ou nada.

        É aqui que mora a garantia do offline: item de venda, movimentos de
        estoque, ingredientes consumidos, ledger de auditoria e as linhas do
        outbox entram **juntos** ou não entram. Não existe estado intermediário
        onde o estoque baixou mas a venda não existe.

        ``IMMEDIATE`` adquire o lock de escrita no BEGIN em vez de na primeira
        escrita, evitando `SQLITE_BUSY` no meio de uma transação já iniciada.
        """
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    # -- utilidades ----------------------------------------------------------- #

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def next_counter(self, connection: sqlite3.Connection, name: str) -> int:
        """Incrementa e devolve uma sequência local, dentro da transação do chamador.

        Usada pelo número da venda (único por terminal) e pelo `seq` do ledger.
        Precisa rodar na mesma transação de quem consome o número, senão dois
        caixas concorrentes recebem o mesmo valor.
        """
        connection.execute(
            "INSERT INTO local_counters (name, value) VALUES (?, 1) "
            "ON CONFLICT (name) DO UPDATE SET value = value + 1",
            (name,),
        )
        row = connection.execute(
            "SELECT value FROM local_counters WHERE name = ?", (name,)
        ).fetchone()
        return int(row["value"])
