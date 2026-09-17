"""Ativação de terminais — o pareamento entre o PDV e a retaguarda.

Este é o único endpoint do sistema que aceita uma requisição **sem token**: é
justamente ele que entrega o token. Daí o cuidado desproporcional ao tamanho do
código.

Controles
---------

* **Código de uso único e de vida curta.** É ditado por telefone para quem está
  no balcão; se vazar, precisa expirar antes de valer alguma coisa.
* **Consumo atômico.** O `UPDATE ... WHERE used_at IS NULL RETURNING` garante
  que duas requisições simultâneas com o mesmo código produzem exatamente uma
  ativação. Ler-e-depois-gravar abriria uma janela de corrida na qual dois
  terminais receberiam a mesma identidade.
* **Comparação em tempo constante.** O código é segredo de curta duração; buscar
  por igualdade no banco vaza timing. Buscamos pelo hash.
* **Limite de tentativas por IP.** Sem isso, um código de 8 caracteres cai por
  força bruta.

O que este endpoint **não** faz
-------------------------------

Não envia nem recebe o `device_secret` (chave HMAC do ledger). Ele é gerado no
terminal e nunca sai de lá — ver `provisioning/secrets.py` no desktop. O que
trafega aqui é identidade e autorização.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/devices", tags=["devices"])

#: Validade do código a partir da geração no painel.
CODE_TTL = timedelta(minutes=15)

#: Tentativas por IP na janela, antes de recusar. Um código de 8 caracteres
#: alfanuméricos tem ~2.8e12 combinações; com este teto a força bruta é inviável
#: dentro dos 15 minutos de validade.
MAX_ATTEMPTS_PER_IP = 10
ATTEMPT_WINDOW = timedelta(minutes=15)


class Fingerprint(BaseModel):
    hostname: str = Field(default="", max_length=128)
    os: str = Field(default="", max_length=128)
    arch: str = Field(default="", max_length=64)


class ActivationRequest(BaseModel):
    activation_code: str = Field(min_length=6, max_length=32)
    fingerprint: Fingerprint = Fingerprint()


class ActivationResponse(BaseModel):
    tenant_id: str
    store_id: str
    device_id: str
    sync_token: str
    store_name: str
    cloud_base_url: str


def code_hash(code: str) -> str:
    """Hash do código, para buscar sem guardar o segredo em texto.

    Um dump do banco não pode entregar códigos ativos. SHA-256 sem sal basta
    aqui: o código é aleatório de alta entropia e vive 15 minutos, então não há
    dicionário a proteger — diferente de uma senha escolhida por gente.
    """
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


async def _check_rate_limit(connection: Any, ip: str) -> None:
    since = datetime.now(timezone.utc) - ATTEMPT_WINDOW
    row = await connection.fetchrow(
        "SELECT COUNT(*) AS total FROM device_activation_attempts "
        "WHERE ip = $1 AND attempted_at > $2",
        ip,
        since,
    )
    if row and int(row["total"]) >= MAX_ATTEMPTS_PER_IP:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas. Aguarde alguns minutos e gere um novo código.",
        )


@router.post("/activate", response_model=ActivationResponse)
async def activate(
    request: ActivationRequest,
    http_request: Request,
    connection: Annotated[Any, Depends(lambda: None)],  # injetar o pool real
) -> ActivationResponse:
    ip = http_request.client.host if http_request.client else "desconhecido"
    await _check_rate_limit(connection, ip)

    await connection.execute(
        "INSERT INTO device_activation_attempts (ip, attempted_at) VALUES ($1, now())",
        ip,
    )

    digest = code_hash(request.activation_code)
    expires_after = datetime.now(timezone.utc) - CODE_TTL

    # Consumo atômico: o WHERE carrega TODAS as condições de validade, e o
    # RETURNING só devolve linha se esta requisição foi a que consumiu. Duas
    # chamadas simultâneas com o mesmo código: uma recebe a linha, a outra
    # recebe None e leva 410.
    row = await connection.fetchrow(
        """
        UPDATE device_activation_codes
           SET used_at = now(),
               used_by_ip = $2
         WHERE code_hash = $1
           AND used_at IS NULL
           AND revoked_at IS NULL
           AND created_at > $3
        RETURNING tenant_id, store_id, device_id
        """,
        digest,
        ip,
        expires_after,
    )

    if row is None:
        # Mensagem deliberadamente igual para código inexistente, expirado e já
        # usado: distingui-los diria a um atacante que ele acertou o código e
        # errou só o tempo.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Código inválido, expirado ou já utilizado. "
            "Gere um novo no painel administrativo.",
        )

    sync_token = secrets.token_urlsafe(48)

    store = await connection.fetchrow(
        "SELECT name, api_base_url FROM stores WHERE id = $1 AND tenant_id = $2",
        row["store_id"],
        row["tenant_id"],
    )

    # O token também é guardado por hash: a retaguarda valida comparando, nunca
    # exibindo. Vazou o banco, os terminais continuam precisando do token cru.
    await connection.execute(
        """
        INSERT INTO devices
            (id, tenant_id, store_id, token_hash, hostname, os, arch,
             activated_at, last_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
        ON CONFLICT (id) DO UPDATE
           SET token_hash   = EXCLUDED.token_hash,
               hostname     = EXCLUDED.hostname,
               os           = EXCLUDED.os,
               arch         = EXCLUDED.arch,
               activated_at = now()
        """,
        row["device_id"],
        row["tenant_id"],
        row["store_id"],
        hashlib.sha256(sync_token.encode("utf-8")).hexdigest(),
        request.fingerprint.hostname,
        request.fingerprint.os,
        request.fingerprint.arch,
    )

    return ActivationResponse(
        tenant_id=str(row["tenant_id"]),
        store_id=str(row["store_id"]),
        device_id=str(row["device_id"]),
        sync_token=sync_token,
        store_name=str(store["name"]) if store else "",
        cloud_base_url=str(store["api_base_url"]) if store and store["api_base_url"] else "",
    )
