"""Contrato do comando remoto — o que a nuvem manda e como o terminal confere.

Este módulo é o único lugar onde a assinatura de um comando é calculada, e
existe separado do serviço porque **os dois lados precisam do mesmo código**:
a nuvem assina, o terminal confere. Duas implementações do mesmo cálculo em
linguagens ou arquivos diferentes divergem em algum detalhe de serialização, e
a divergência aparece como "o terminal parou de obedecer ao painel" numa
sexta-feira à noite.

Por que assinar, se a chamada já é autenticada
----------------------------------------------

O token de sincronização prova que *alguém com a credencial do terminal* está
falando. A assinatura prova que **a nuvem emitiu aquele comando exato** para
**aquele terminal**. São garantias diferentes, e a segunda é a que importa
aqui: sem ela, quem conseguisse injetar resposta no canal de sync passaria a
conceder descontos, e o terminal obedeceria por não ter como distinguir.

A chave é o `device_secret` — a mesma do ledger de auditoria, provisionada na
ativação e guardada na DPAPI. É simétrica: a nuvem a conhece porque foi ela
quem provisionou.

O que a assinatura cobre
------------------------

Tudo que muda o efeito do comando: o identificador, o terminal alvo, o tipo, o
payload canônico e o instante de emissão. Deixar o `device_id` de fora deixaria
um comando legítimo de uma loja ser replayado noutra; deixar o `issued_at` de
fora deixaria um comando antigo capturado ser reaplicado para sempre.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pdv.domain.models import utc_now


class CommandKind(Enum):
    """Os únicos comandos que o terminal aceita de fora.

    A lista é curta de propósito: cada item aqui é uma permissão nova dada a
    quem comprometer o painel. Abrir gaveta e reimprimir cupom foram deixados
    de fora — são as duas operações que um atacante remoto mais gostaria de
    ter, e nenhuma delas resolve um problema que o telefone não resolva.
    """

    APPLY_DISCOUNT = "apply_discount"
    CANCEL_ITEM = "cancel_item"


class CommandStatus(Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REFUSED = "refused"


#: Janela de validade de um comando. Um comando capturado do canal e reaplicado
#: semanas depois — quando o pedido já fechou e o terminal já esqueceu — não
#: pode ter efeito. O prazo é generoso porque o terminal pode estar offline: um
#: PDV com a internet caída desde a manhã ainda deve receber o desconto que o
#: gerente concedeu ao meio-dia.
MAX_COMMAND_AGE = timedelta(hours=12)

#: Tolerância para relógio adiantado no servidor. Sem ela, alguns segundos de
#: drift fariam o terminal recusar comandos legítimos como "emitidos no futuro".
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    """Um comando emitido pelo painel para um terminal específico."""

    command_uuid: str
    tenant_id: str
    store_id: str
    device_id: str
    kind: CommandKind
    payload: dict[str, Any]
    issued_by_user_id: str
    issued_by_name: str
    issued_at: str
    signature: str

    def signing_material(self) -> bytes:
        return _material(
            command_uuid=self.command_uuid,
            device_id=self.device_id,
            kind=self.kind.value,
            payload=self.payload,
            issued_at=self.issued_at,
        )


def canonical_payload(payload: dict[str, Any]) -> str:
    """JSON determinístico: chaves ordenadas, sem espaço supérfluo.

    A assinatura é sobre *bytes*. Se os dois lados serializarem o mesmo dicionário
    com ordens diferentes, a conferência falha sem que nada esteja errado.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _material(
    *,
    command_uuid: str,
    device_id: str,
    kind: str,
    payload: dict[str, Any],
    issued_at: str,
) -> bytes:
    # O separador `\x1f` (unit separator) não aparece em uuid, em nome de
    # comando nem em ISO-8601. Concatenar sem separador deixaria dois campos
    # diferentes produzirem o mesmo material — e duas mensagens distintas com a
    # mesma assinatura é exatamente o que uma assinatura existe para impedir.
    parts = (command_uuid, device_id, kind, canonical_payload(payload), issued_at)
    return "\x1f".join(parts).encode("utf-8")


def sign_command(
    *,
    secret: bytes,
    command_uuid: str,
    device_id: str,
    kind: str,
    payload: dict[str, Any],
    issued_at: str,
) -> str:
    """Assina um comando. Usado pela nuvem ao emitir."""
    material = _material(
        command_uuid=command_uuid,
        device_id=device_id,
        kind=kind,
        payload=payload,
        issued_at=issued_at,
    )
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def verify_signature(command: RemoteCommand, secret: bytes) -> bool:
    """Confere a assinatura em tempo constante."""
    expected = hmac.new(secret, command.signing_material(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, command.signature)


def is_fresh(issued_at: str, *, now: datetime | None = None) -> bool:
    """O comando ainda está dentro da janela de validade?

    Data ilegível conta como **não fresca**. Um campo que não dá para
    interpretar não pode virar permissão para agir.
    """
    reference = now or utc_now()
    try:
        issued = datetime.fromisoformat(issued_at)
    except ValueError:
        return False

    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=reference.tzinfo)

    if issued - reference > CLOCK_SKEW_TOLERANCE:
        return False
    return reference - issued <= MAX_COMMAND_AGE


__all__ = [
    "CLOCK_SKEW_TOLERANCE",
    "MAX_COMMAND_AGE",
    "CommandKind",
    "CommandStatus",
    "RemoteCommand",
    "canonical_payload",
    "is_fresh",
    "sign_command",
    "verify_signature",
]
