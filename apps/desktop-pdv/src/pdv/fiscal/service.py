"""Reserva fiscal atômica e idempotente por terminal.

Esta primeira fatia não assina XML nem afirma autorização da SEFAZ. Ela resolve
o problema anterior e mais perigoso: dois terminais jamais compartilham série,
e repetir a tentativa para a mesma venda devolve o mesmo número em vez de
consumir outro. A transmissão será um adaptador posterior sobre estes estados.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.domain.models import EntityId, iso, new_id, utc_now

FiscalEnvironment = Literal["homologation", "production"]
FiscalStatus = Literal["pending", "contingency_pending", "authorized", "rejected", "canceled"]


class FiscalError(RuntimeError):
    """Configuração ou venda incompatível com a emissão fiscal."""


class FiscalNotRequired(FiscalError):
    """A venda fechou com total zero: não há documento fiscal a emitir.

    Acontece com desconto de 100% (cortesia, degustação, consumo da equipe) ou
    com produto de preço zero. A NFC-e documenta uma operação com valor; uma
    nota de R$ 0,00 não tem o que tributar e seria rejeitada pela SEFAZ depois
    de já ter consumido um número da série — que então precisaria de
    inutilização formal.

    É uma exceção **própria**, e não `FiscalError` genérico, porque quem chama
    precisa tratá-la como desfecho normal ("venda concluída, sem nota"), nunca
    como falha a repetir: tentar de novo não muda o total.

    A trilha não se perde: o desconto de 100% já passou pela autorização de
    gerente/dono e está no ledger de auditoria. É ali, e não numa nota de valor
    zero, que se confere quem liberou a cortesia.
    """


@dataclass(frozen=True, slots=True)
class FiscalDocument:
    id: EntityId
    order_id: EntityId
    model: int
    series: int
    number: int
    environment: FiscalEnvironment
    emission_type: Literal["normal", "offline_contingency"]
    status: FiscalStatus
    issued_at: str
    contingency_reason: str | None = None


class FiscalService:
    def __init__(self, database: Database, config: AppConfig) -> None:
        self._db = database
        self._config = config

    def configure_series(
        self, *, series: int, model: int = 65,
        environment: FiscalEnvironment = "homologation",
    ) -> None:
        if not 1 <= series <= 999:
            raise FiscalError("A série fiscal deve estar entre 1 e 999.")
        if model not in (59, 65):
            raise FiscalError("Modelo fiscal não suportado.")
        now = iso(utc_now())
        with self._db.transaction() as connection:
            owner = connection.execute(
                "SELECT device_id FROM fiscal_series WHERE tenant_id=? AND store_id=? "
                "AND model=? AND series=?",
                (self._config.tenant_id, self._config.store_id, model, series),
            ).fetchone()
            if owner is not None and owner["device_id"] != self._config.device_id:
                raise FiscalError("Esta série fiscal já pertence a outro terminal.")
            row = connection.execute(
                "SELECT series,environment FROM fiscal_series WHERE tenant_id=? "
                "AND store_id=? AND device_id=? AND model=?",
                (self._config.tenant_id, self._config.store_id,
                 self._config.device_id, model),
            ).fetchone()
            if row is not None and int(row["series"]) != series:
                used = connection.execute(
                    "SELECT 1 FROM fiscal_documents WHERE tenant_id=? AND store_id=? "
                    "AND device_id=? AND model=? LIMIT 1",
                    (self._config.tenant_id, self._config.store_id,
                     self._config.device_id, model),
                ).fetchone()
                if used is not None:
                    raise FiscalError("A série já emitiu documentos e não pode ser trocada.")
            connection.execute(
                "INSERT INTO fiscal_series "
                "(id,tenant_id,store_id,device_id,model,series,next_number,environment,updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?) ON CONFLICT(tenant_id,store_id,device_id,model) "
                "DO UPDATE SET series=excluded.series,environment=excluded.environment,"
                "updated_at=excluded.updated_at",
                (new_id(), self._config.tenant_id, self._config.store_id,
                 self._config.device_id, model, series, environment, now),
            )

    def requires_document(self, order_id: EntityId) -> bool:
        """A venda precisa de NFC-e? Falso quando o total fechou em zero.

        Consultado ANTES de falar com a nuvem: uma venda de cortesia feita com
        a internet fora não pode cair na série de contingência só porque a
        pergunta "precisa de nota?" dependia da rede para ser respondida.
        """
        row = self._db.query_one(
            "SELECT total_cents FROM orders WHERE id=? AND tenant_id=?",
            (order_id, self._config.tenant_id),
        )
        if row is None:
            raise FiscalError("Venda não encontrada neste tenant.")
        return _requires_document(int(row["total_cents"]))

    def reserve(
        self, *, order_id: EntityId, online: bool, model: int = 65,
        contingency_reason: str | None = None,
    ) -> FiscalDocument:
        with self._db.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM fiscal_documents WHERE tenant_id=? AND order_id=? AND model=?",
                (self._config.tenant_id, order_id, model),
            ).fetchone()
            if existing is not None:
                return _document(existing)

            order = connection.execute(
                "SELECT tenant_id,store_id,device_id,status,total_cents FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
            if order is None or order["tenant_id"] != self._config.tenant_id:
                raise FiscalError("Venda não encontrada neste tenant.")
            if order["store_id"] != self._config.store_id or order["device_id"] != self._config.device_id:
                raise FiscalError("A venda pertence a outro terminal; emissão recusada.")
            if order["status"] != "paid":
                raise FiscalError("Somente uma venda paga pode reservar documento fiscal.")
            # Antes de tocar na série: uma venda de total zero não pode consumir
            # número de contingência, nem por engano de quem chamou.
            if not _requires_document(int(order["total_cents"])):
                raise FiscalNotRequired(
                    "Venda com total zero (desconto de 100% ou item sem preço): "
                    "não se emite NFC-e."
                )

            series_row = connection.execute(
                "SELECT * FROM fiscal_series WHERE tenant_id=? AND store_id=? "
                "AND device_id=? AND model=?",
                (self._config.tenant_id, self._config.store_id,
                 self._config.device_id, model),
            ).fetchone()
            if series_row is None:
                raise FiscalError("Série fiscal deste terminal ainda não foi configurada.")

            number = int(series_row["next_number"])
            now = iso(utc_now())
            document_id, client_uuid = new_id(), new_id()
            emission_type = "normal" if online else "offline_contingency"
            status = "pending" if online else "contingency_pending"
            reason = None if online else (contingency_reason or "sem conectividade com a autorização")
            connection.execute(
                "UPDATE fiscal_series SET next_number=next_number+1,updated_at=? WHERE id=?",
                (now, series_row["id"]),
            )
            connection.execute(
                "INSERT INTO fiscal_documents "
                "(id,tenant_id,store_id,device_id,order_id,model,series,number,environment,"
                "emission_type,status,contingency_reason,issued_at,updated_at,client_uuid,is_synced) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (document_id, self._config.tenant_id, self._config.store_id,
                 self._config.device_id, order_id, model, int(series_row["series"]),
                 number, series_row["environment"], emission_type, status, reason,
                 now, now, client_uuid),
            )
            connection.execute(
                "INSERT INTO fiscal_events "
                "(id,tenant_id,fiscal_document_id,event_type,payload_json,created_at,client_uuid,is_synced) "
                "VALUES (?,?,?,'reserved',?, ?,?,0)",
                (new_id(), self._config.tenant_id, document_id,
                 '{"source":"desktop","atomic":true}', now, new_id()),
            )
            row = connection.execute(
                "SELECT * FROM fiscal_documents WHERE id=?", (document_id,),
            ).fetchone()
            assert row is not None
            return _document(row)


def _requires_document(total_cents: int) -> bool:
    """Zero não emite; negativo é defeito, e defeito não vira nota nem silêncio."""
    if total_cents < 0:
        raise FiscalError("Venda com total negativo: corrija antes de qualquer emissão.")
    return total_cents > 0


def _document(item: sqlite3.Row) -> FiscalDocument:
    return FiscalDocument(
        id=EntityId(item["id"]), order_id=EntityId(item["order_id"]),
        model=int(item["model"]), series=int(item["series"]), number=int(item["number"]),
        environment=item["environment"], emission_type=item["emission_type"],
        status=item["status"], issued_at=item["issued_at"],
        contingency_reason=item["contingency_reason"],
    )
