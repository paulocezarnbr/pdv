from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fiscal_service.models import FiscalResult


class ResultStore:
    """Segunda trava idempotente, independente do Postgres/Next."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS fiscal_results (
                    request_uuid TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('processing','settled')),
                    result_json TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def claim(self, request_uuid: str, document_id: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO fiscal_results(request_uuid,document_id,state) "
                "VALUES (?,?,'processing')", (request_uuid, document_id),
            )
            return cursor.rowcount > 0

    def settle(self, request_uuid: str, result: FiscalResult) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE fiscal_results SET state='settled',result_json=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE request_uuid=? AND state='processing'",
                (result.model_dump_json(), request_uuid),
            )

    def get(self, request_uuid: str) -> FiscalResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT state,result_json FROM fiscal_results WHERE request_uuid=?",
                (request_uuid,),
            ).fetchone()
        if row is None or row[0] != "settled" or not row[1]:
            return None
        return FiscalResult.model_validate(json.loads(row[1]))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10)
