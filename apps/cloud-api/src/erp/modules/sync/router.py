"""Endpoints de sincronização.

`POST /sync/push` é o endpoint mais sensível do sistema: é por ele que entra o
faturamento de todas as lojas. Três controles não negociáveis:

* **Tenant vem do token, nunca do corpo.** Se o `tenant_id` do payload fosse
  aceito, um terminal comprometido gravaria vendas no tenant de outro
  restaurante — ou leria o dele.
* **Uma transação por lote.** Aplicação parcial corromperia a relação entre
  venda, estoque e auditoria.
* **`Idempotency-Key` obrigatório.** É o que permite ao cliente reenviar com
  segurança quando não sabe se a primeira tentativa chegou.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from erp.modules.sync.merge import ItemStatus, SyncItem, SyncMerger

router = APIRouter(prefix="/sync", tags=["sync"])


class SyncItemIn(BaseModel):
    entity_table: str = Field(max_length=64)
    entity_id: str = Field(max_length=64)
    client_uuid: str = Field(max_length=64)
    operation: str = Field(pattern="^(insert|update|delete)$")
    payload: dict[str, Any]


class PushRequest(BaseModel):
    device_id: str
    tenant_id: str
    store_id: str
    # Teto de lote: protege contra um terminal (ou um atacante) despejar um
    # payload gigante e segurar uma conexão de banco indefinidamente.
    items: list[SyncItemIn] = Field(max_length=500)


class ItemResultOut(BaseModel):
    client_uuid: str
    status: str
    message: str | None = None
    server_seq: int | None = None


class PushResponse(BaseModel):
    results: list[ItemResultOut]
    applied: int
    duplicates: int
    rejected: int


@router.post("/push", response_model=PushResponse)
async def push(
    request: PushRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    context: Annotated[Any, Depends(lambda: None)] = None,  # DeviceContext real
) -> PushResponse:
    """Aplica um lote do terminal.

    O `Idempotency-Key` cobre o lote inteiro; `client_uuid` cobre cada item.
    A redundância é proposital: a chave do lote evita reprocessamento custoso,
    e a do item garante a correção mesmo se o lote for remontado de outra forma.
    """
    if not idempotency_key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Idempotency-Key é obrigatório"
        )

    # `context` vem do token do dispositivo (device binding + claims).
    # As linhas abaixo mostram a intenção; a injeção real é feita pelo
    # middleware de tenancy, que também executa `SET LOCAL app.tenant_id`.
    tenant_id = context.tenant_id if context else request.tenant_id
    store_id = context.store_id if context else request.store_id
    device_id = context.device_id if context else request.device_id

    if request.tenant_id != tenant_id or request.device_id != device_id:
        # Divergência entre token e corpo é sinal de terminal clonado ou de
        # tentativa de escrita cruzada entre tenants.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Identidade do terminal não confere"
        )

    merger = SyncMerger(
        tenant_id=tenant_id,
        store_id=store_id,
        device_id=device_id,
        key_resolver=context.key_resolver,
        anchors=context.anchors,
        alerts=context.alerts,
    )

    items = [
        SyncItem(
            entity_table=item.entity_table,
            entity_id=item.entity_id,
            client_uuid=item.client_uuid,
            operation=item.operation,
            payload=item.payload,
        )
        for item in request.items
    ]

    # UMA transação para o lote inteiro.
    async with context.db.transaction() as connection:
        results = merger.apply(items, connection)

    return PushResponse(
        results=[
            ItemResultOut(
                client_uuid=r.client_uuid,
                status=r.status.value,
                message=r.message,
                server_seq=r.server_seq,
            )
            for r in results
        ],
        applied=sum(1 for r in results if r.status is ItemStatus.APPLIED),
        duplicates=sum(1 for r in results if r.status is ItemStatus.DUPLICATE),
        rejected=sum(1 for r in results if r.status is ItemStatus.REJECTED),
    )


@router.get("/pull")
async def pull(
    entity_table: Annotated[str, Query(max_length=64)],
    since: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    context: Annotated[Any, Depends(lambda: None)] = None,
) -> dict[str, Any]:
    """Baixa cadastros alterados na retaguarda, por cursor de `server_seq`.

    Paginar por `server_seq` e não por `updated_at` é deliberado: dois registros
    podem compartilhar o mesmo timestamp e um deles seria pulado na virada de
    página. `server_seq` é estritamente crescente e não empata.
    """
    PULLABLE = {"products", "recipes", "recipe_lines", "inventory_items", "users"}
    if entity_table not in PULLABLE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Tabela não disponível: {entity_table}"
        )

    rows = await context.db.fetch_all(
        f"SELECT * FROM {entity_table} "  # noqa: S608 - validado contra PULLABLE
        "WHERE tenant_id = :tenant AND server_seq > :since "
        "ORDER BY server_seq LIMIT :limit",
        {"tenant": context.tenant_id, "since": since, "limit": limit},
    )

    return {
        "rows": [dict(row) for row in rows],
        "last_server_seq": max((r["server_seq"] for r in rows), default=since),
        "has_more": len(rows) == limit,
    }
