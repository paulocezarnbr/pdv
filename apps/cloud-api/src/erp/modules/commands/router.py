"""Comandos do painel para o terminal — o lado da nuvem.

> ⚠️ Este é o **único** caminho pelo qual a nuvem manda o PDV fazer alguma
> coisa. Todo o resto do sistema flui na direção contrária. Quem comprometer
> este endpoint passa a conceder descontos e cancelar itens em todas as lojas
> ao mesmo tempo, sem pisar em nenhuma delas.

A divisão de responsabilidade com o terminal
--------------------------------------------

A nuvem **assina** e **entrega**. O terminal **confere** e **decide**. Não há
redundância aqui: as duas metades checam coisas diferentes, e a do terminal é
a que vale, porque é ela que continua valendo quando esta API é a parte
comprometida.

* A nuvem confere quem é o emissor, se o perfil dele pode aquilo e se o
  terminal alvo pertence ao tenant. Isso evita emitir besteira.
* O terminal confere a **assinatura**, a validade, o teto do perfil lido da
  **réplica local** e o estado do pedido. Isso evita obedecer besteira.

Se a segunda metade dependesse de a primeira ter feito o trabalho dela, um
painel comprometido teria poder total. Ver `remote/commands.py` no desktop.

Entrega não consome
-------------------

`GET /commands/pending` devolve o mesmo comando quantas vezes for preciso, até
o terminal relatar o que fez com ele. Consumir na entrega perderia o comando
de vez se o terminal morresse entre receber e gravar na inbox — e perder em
silêncio é pior que entregar duas vezes, porque a segunda entrega colide no
`command_uuid` do terminal e vira no-op.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/commands", tags=["commands"])

#: Os únicos comandos que o terminal aceita. A lista é curta de propósito:
#: cada item aqui é uma permissão nova dada a quem comprometer o painel. Abrir
#: gaveta e reimprimir cupom ficaram de fora — são as duas operações que um
#: atacante remoto mais gostaria de ter, e nenhuma delas resolve um problema
#: que o telefone não resolva.
KINDS = frozenset({"apply_discount", "cancel_item"})

#: Janela de validade, espelhando `MAX_COMMAND_AGE` do terminal. Emitir com
#: prazo maior do que o terminal aceita só produziria comando nascido morto.
MAX_AGE = timedelta(hours=12)

#: Teto de comandos por operador na janela. Não é defesa contra o gerente
#: distraído — é o que limita o estrago de uma credencial vazada ao que dá para
#: reverter numa manhã, em vez de uma noite inteira de descontos.
MAX_PER_OPERATOR = 60
RATE_WINDOW = timedelta(minutes=10)


# --------------------------------------------------------------------------- #
# Emissão (usada pelo painel)
# --------------------------------------------------------------------------- #


class IssueRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=32)
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Gerado pelo painel, como o `client_uuid` é gerado pelo celular do garçom.
    #: Dois cliques no botão viram um comando só, e não dois descontos.
    command_uuid: str = Field(min_length=8, max_length=64)


class CommandOut(BaseModel):
    command_uuid: str
    tenant_id: str
    store_id: str
    device_id: str
    kind: str
    payload: dict[str, Any]
    issued_by_user_id: str
    issued_by_name: str
    issued_at: str
    signature: str


@router.post("/issue", response_model=CommandOut)
async def issue(
    request: IssueRequest,
    context: Annotated[Any, Depends(lambda: None)] = None,  # UserContext real
) -> CommandOut:
    """Emite um comando assinado para um terminal.

    Quem chama é uma **pessoa** logada no painel, não um terminal. Por isso o
    contexto aqui é de usuário: o `issued_by_user_id` sai do token e nunca do
    corpo, senão bastaria trocar um campo para emitir em nome do dono.
    """
    if request.kind not in KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Comando não suportado: {request.kind}"
        )

    _require_authorizer(context, request)
    await _check_rate_limit(context)

    device = await context.db.fetch_one(
        "SELECT id, store_id, secret_ref FROM devices "
        " WHERE id = :id AND tenant_id = :tenant AND revoked_at IS NULL",
        {"id": request.device_id, "tenant": context.tenant_id},
    )
    if device is None:
        # 404 e não 403: o terminal não existe *neste tenant*. Distinguir os
        # dois casos contaria a um atacante quais device_id existem por aí.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Terminal não encontrado")

    issued_at = datetime.now(timezone.utc).isoformat()
    secret = await context.key_resolver.device_secret(device["id"])
    signature = sign_command(
        secret=secret,
        command_uuid=request.command_uuid,
        device_id=device["id"],
        kind=request.kind,
        payload=request.payload,
        issued_at=issued_at,
    )

    async with context.db.transaction() as connection:
        # `ON CONFLICT DO NOTHING` + leitura: dois cliques no painel, ou um
        # retry do navegador, devolvem o comando já emitido em vez de emitir
        # outro. A assinatura é determinística para o mesmo conteúdo, mas o
        # `issued_at` não — sem esta trava, o segundo clique produziria um
        # comando novo, com uuid igual e assinatura diferente.
        await connection.execute(
            "INSERT INTO remote_commands "
            "  (command_uuid, tenant_id, store_id, device_id, kind, payload_json, "
            "   issued_by_user_id, issued_by_name, issued_at, signature, status) "
            "VALUES (:uuid, :tenant, :store, :device, :kind, :payload, "
            "        :user, :name, :issued_at, :signature, 'pending') "
            "ON CONFLICT (command_uuid) DO NOTHING",
            {
                "uuid": request.command_uuid,
                "tenant": context.tenant_id,
                "store": device["store_id"],
                "device": device["id"],
                "kind": request.kind,
                "payload": json.dumps(
                    request.payload, sort_keys=True, ensure_ascii=False
                ),
                "user": context.user_id,
                "name": context.user_name,
                "issued_at": issued_at,
                "signature": signature,
            },
        )
        row = await connection.fetch_one(
            "SELECT * FROM remote_commands WHERE command_uuid = :uuid",
            {"uuid": request.command_uuid},
        )

    return _to_out(row)


# --------------------------------------------------------------------------- #
# Entrega (usada pelo terminal)
# --------------------------------------------------------------------------- #


@router.get("/pending")
async def pending(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    context: Annotated[Any, Depends(lambda: None)] = None,  # DeviceContext real
) -> dict[str, Any]:
    """Entrega os comandos que ainda não foram decididos por este terminal.

    O `device_id` vem do **token**, nunca da query: aceitá-lo do cliente deixaria
    um terminal comprometido ler os comandos endereçados a outro — e um comando
    é assinado, então lê-lo é o primeiro passo para replayá-lo.
    """
    rows = await context.db.fetch_all(
        "SELECT * FROM remote_commands "
        " WHERE tenant_id = :tenant AND device_id = :device AND status = 'pending' "
        "   AND issued_at > :floor "
        " ORDER BY issued_at LIMIT :limit",
        {
            "tenant": context.tenant_id,
            "device": context.device_id,
            # Comando vencido não é entregue: o terminal o recusaria de
            # qualquer jeito, e a recusa gastaria um evento de auditoria com
            # severidade que não é a dele.
            "floor": (datetime.now(timezone.utc) - MAX_AGE).isoformat(),
            "limit": limit,
        },
    )

    # A entrega é registrada mas **não** consome: ver o cabeçalho do módulo.
    if rows:
        await context.db.execute(
            "UPDATE remote_commands SET delivered_at = COALESCE(delivered_at, :now), "
            "  delivery_count = delivery_count + 1 "
            " WHERE command_uuid = ANY(:uuids)",
            {
                "now": datetime.now(timezone.utc).isoformat(),
                "uuids": [r["command_uuid"] for r in rows],
            },
        )

    return {"commands": [_to_out(row).model_dump() for row in rows]}


class ResultIn(BaseModel):
    command_uuid: str = Field(min_length=8, max_length=64)
    status: str = Field(pattern="^(applied|refused)$")
    message: str = Field(default="", max_length=500)
    settled_at: str = Field(default="", max_length=64)


class ResultsRequest(BaseModel):
    tenant_id: str
    store_id: str
    device_id: str
    results: list[ResultIn] = Field(max_length=200)


@router.post("/results")
async def results(
    request: ResultsRequest,
    context: Annotated[Any, Depends(lambda: None)] = None,  # DeviceContext real
) -> dict[str, Any]:
    """Recebe o que o terminal fez com cada comando.

    Responde **quais** foram aceitos, e não um "ok" genérico. O terminal só
    tira da fila de relato o que a nuvem nomear; uma confirmação em bloco
    esconderia uma gravação parcial, e o painel ficaria mostrando `pendente`
    num comando já aplicado — que é o estado em que alguém reemite o desconto
    na mão.
    """
    if request.device_id != context.device_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Identidade do terminal não confere"
        )

    accepted: list[str] = []
    async with context.db.transaction() as connection:
        for result in request.results:
            # `status = 'pending'` na cláusula: o terminal é a autoridade sobre
            # o que aconteceu, mas só na **primeira** vez que conta. Reescrever
            # um resultado já gravado deixaria um terminal comprometido apagar
            # o registro de um cancelamento que ele mesmo aplicou.
            updated = await connection.execute(
                "UPDATE remote_commands "
                "   SET status = :status, result_message = :message, "
                "       settled_at = :settled_at, reported_at = :now "
                " WHERE command_uuid = :uuid AND tenant_id = :tenant "
                "   AND device_id = :device AND status = 'pending'",
                {
                    "status": result.status,
                    "message": result.message,
                    "settled_at": result.settled_at
                    or datetime.now(timezone.utc).isoformat(),
                    "now": datetime.now(timezone.utc).isoformat(),
                    "uuid": result.command_uuid,
                    "tenant": context.tenant_id,
                    "device": context.device_id,
                },
            )
            # Zero linhas significa que o resultado já tinha chegado antes. Isso
            # é sucesso, não erro: o relato anterior chegou e só a resposta se
            # perdeu. Recusar aqui faria o terminal reavisar para sempre.
            accepted.append(result.command_uuid)
            del updated

    return {"accepted": accepted}


# --------------------------------------------------------------------------- #
# Assinatura — o mesmo cálculo do terminal
# --------------------------------------------------------------------------- #
#
# Este bloco é uma cópia deliberada de `pdv/remote/protocol.py`. Duas
# implementações do mesmo HMAC divergem em algum detalhe de serialização, e a
# divergência aparece como "o terminal parou de obedecer ao painel" numa
# sexta-feira à noite. O teste de contrato compara as duas byte a byte — se
# alguém mexer numa, a outra quebra no CI e não na loja.


def canonical_payload(payload: dict[str, Any]) -> str:
    """JSON determinístico: chaves ordenadas, sem espaço supérfluo."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign_command(
    *,
    secret: bytes,
    command_uuid: str,
    device_id: str,
    kind: str,
    payload: dict[str, Any],
    issued_at: str,
) -> str:
    """Assina o comando. O separador `\\x1f` não aparece em uuid nem em ISO-8601."""
    material = "\x1f".join(
        (command_uuid, device_id, kind, canonical_payload(payload), issued_at)
    ).encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Apoio
# --------------------------------------------------------------------------- #


def _require_authorizer(context: Any, request: IssueRequest) -> None:
    """Confere o perfil de quem emite, **antes** de assinar.

    Não substitui a conferência do terminal — duplica-a de propósito. Esta
    evita emitir um comando nascido para ser recusado; a do terminal é a que
    continua valendo quando esta API é a parte comprometida.
    """
    if not getattr(context, "can_authorize", False):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Seu perfil não autoriza operações remotas.",
        )

    if request.kind != "apply_discount":
        return

    try:
        percent = Decimal(str(request.payload.get("percent")))
    except (InvalidOperation, TypeError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Percentual inválido"
        ) from None

    ceiling = Decimal(str(getattr(context, "max_discount_percent", 0) or 0))
    if percent <= 0 or percent > ceiling:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Você pode conceder até {ceiling}% — o pedido é de {percent}%.",
        )

    if not str(request.payload.get("reason") or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Informe o motivo")


async def _check_rate_limit(context: Any) -> None:
    """Teto por operador na janela.

    Limita o estrago de uma credencial vazada ao que dá para reverter numa
    manhã, em vez de uma noite inteira de descontos em todas as lojas.
    """
    recent = await context.db.fetch_val(
        "SELECT COUNT(*) FROM remote_commands "
        " WHERE tenant_id = :tenant AND issued_by_user_id = :user "
        "   AND issued_at > :since",
        {
            "tenant": context.tenant_id,
            "user": context.user_id,
            "since": (datetime.now(timezone.utc) - RATE_WINDOW).isoformat(),
        },
    )
    if int(recent or 0) >= MAX_PER_OPERATOR:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Comandos demais em pouco tempo. Se não foi você, troque sua senha.",
        )


def _to_out(row: Any) -> CommandOut:
    return CommandOut(
        command_uuid=row["command_uuid"],
        tenant_id=row["tenant_id"],
        store_id=row["store_id"],
        device_id=row["device_id"],
        kind=row["kind"],
        payload=json.loads(row["payload_json"]),
        issued_by_user_id=row["issued_by_user_id"],
        issued_by_name=row["issued_by_name"] or "",
        issued_at=row["issued_at"],
        signature=row["signature"],
    )
