"""Ativação do terminal — o pareamento com a retaguarda.

O lojista digita um **código de ativação** curto, gerado no painel administrativo
e válido por poucos minutos. Em troca, o terminal recebe a identidade com que vai
existir na nuvem: `tenant_id`, `store_id`, `device_id` e o token de
sincronização.

Por que um código curto e não o token direto
--------------------------------------------

O token de sincronização é longo e vitalício. Um código de uso único e de vida
curta pode ser ditado por telefone sem que isso comprometa nada de duradouro: se
vazar, ele expira antes de servir para algo, e queimá-lo custa um clique no
painel.

O segredo do ledger viaja uma vez, e só aqui
--------------------------------------------

O `device_secret` (chave HMAC do ledger) é gerado localmente — ver
`secrets.py` — e segue para a nuvem **uma única vez**, nesta chamada, por
HTTPS. A nuvem precisa dele: a cadeia de auditoria é HMAC, simétrica, e a
regra 3 da sincronização é justamente o servidor reconferir cada elo em vez de
aceitar o autoatestado do caixa. Ela o guarda só para conferir, nunca o
devolve, e uma reativação **não** o troca (`ON CONFLICT DO NOTHING`).

Esta documentação dizia o contrário — que a chave nunca saía da máquina —, e a
ativação de fato não a enviava. A nuvem, sem a chave, respondia 409 a todo
lote: nenhum terminal ativado de verdade conseguia sincronizar.

A regra que impede o pior erro de campo
---------------------------------------

**Um terminal com vendas não sincronizadas não pode trocar de tenant.** Aquelas
vendas foram registradas sob o CNPJ antigo; reapontar o terminal antes de
esvaziar a fila faria o faturamento de uma loja desembarcar na outra. É o tipo
de erro que ninguém percebe no dia e que aparece na conciliação fiscal do mês.
Por isso `activate()` recusa a troca com fila pendente, e o operador precisa
sincronizar (ou o suporte precisa decidir conscientemente) antes.
"""

from __future__ import annotations

import logging
import platform
import re
from dataclasses import dataclass
from typing import Protocol

from pdv.config import cloud_api_root
from pdv.data.database import Database
from pdv.data.settings import SettingsStore
from pdv.domain.errors import PdvError
from pdv.provisioning.secrets import SecretVault
from pdv.sync.outbox import OutboxReader

logger = logging.getLogger(__name__)

#: Nome do segredo no cofre. O token nunca entra em `device_settings`.
SYNC_TOKEN_NAME = "sync_token"

#: Códigos são digitados por quem está no balcão, não colados. Aceitamos
#: separadores e caixa livre para que "a1b2-c3d4" e "A1B2C3D4" sejam o mesmo.
_CODE_CLEANUP = re.compile(r"[^A-Za-z0-9]")

MIN_CODE_LENGTH = 6
MAX_CODE_LENGTH = 32


class ActivationError(PdvError):
    """Falha na ativação do terminal."""


class ActivationRefused(ActivationError):
    """O servidor recusou o código (expirado, já usado ou inexistente)."""


class ActivationBlocked(ActivationError):
    """A ativação é possível, mas seria destrutiva agora."""


@dataclass(frozen=True, slots=True)
class ActivationResult:
    tenant_id: str
    store_id: str
    device_id: str
    sync_token: str
    store_name: str = ""
    cloud_base_url: str = ""


class ActivationTransport(Protocol):
    """Contrato do canal de ativação. Injetável para testar sem rede."""

    def activate(
        self, code: str, fingerprint: dict[str, str], device_secret: bytes
    ) -> ActivationResult: ...


def normalize_code(raw: str) -> str:
    """Limpa o código digitado.

    Raises:
        ActivationError: se o que sobrou não tem tamanho plausível.
    """
    cleaned = _CODE_CLEANUP.sub("", raw).upper()
    if not MIN_CODE_LENGTH <= len(cleaned) <= MAX_CODE_LENGTH:
        raise ActivationError(
            f"Código de ativação inválido: esperados entre {MIN_CODE_LENGTH} e "
            f"{MAX_CODE_LENGTH} caracteres, recebidos {len(cleaned)}."
        )
    return cleaned


def machine_fingerprint() -> dict[str, str]:
    """Identificação da máquina, para o painel mostrar qual terminal é qual.

    É um rótulo de conveniência, **não** um mecanismo de segurança: tudo aqui é
    forjável por quem controla o processo. Quem autentica é o token.
    """
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
    }


class HttpActivationTransport:
    """Fala com `POST {cloud}/api/devices/activate`."""

    def __init__(self, base_url: str, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def activate(
        self, code: str, fingerprint: dict[str, str], device_secret: bytes
    ) -> ActivationResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ActivationError(
                "httpx não instalado — execute: pip install httpx"
            ) from exc

        try:
            response = httpx.post(
                f"{cloud_api_root(self._base_url)}/devices/activate",
                json={
                    "activation_code": code,
                    "fingerprint": fingerprint,
                    "device_secret_hex": device_secret.hex(),
                },
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - httpx tem muitas subclasses
            raise ActivationError(
                f"Não foi possível falar com a retaguarda: {exc}"
            ) from exc

        # 4xx é veredito: reenviar o mesmo código dá o mesmo resultado. 5xx é
        # transitório e vale nova tentativa — a distinção evita que o técnico
        # queime um código válido insistindo contra um servidor fora do ar.
        if response.status_code in (400, 401, 403, 404, 409, 410):
            raise ActivationRefused(_refusal_message(response))
        if response.status_code >= 400:
            raise ActivationError(
                f"A retaguarda respondeu {response.status_code}. Tente novamente "
                "em alguns minutos."
            )

        return _parse_activation(response.json(), self._base_url)


def _refusal_message(response: object) -> str:
    detail = ""
    try:
        body = response.json()  # type: ignore[attr-defined]
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or "")
    except Exception:  # noqa: BLE001
        detail = ""
    return detail or (
        "Código recusado. Confira se foi digitado corretamente e se ainda não "
        "expirou — gere um novo no painel administrativo."
    )


def _parse_activation(body: object, base_url: str) -> ActivationResult:
    if not isinstance(body, dict):
        raise ActivationError("Resposta de ativação em formato inesperado.")

    required = ("tenant_id", "store_id", "device_id", "sync_token")
    missing = [field for field in required if not body.get(field)]
    if missing:
        raise ActivationError(
            f"Resposta de ativação incompleta: falta {', '.join(missing)}."
        )

    return ActivationResult(
        tenant_id=str(body["tenant_id"]),
        store_id=str(body["store_id"]),
        device_id=str(body["device_id"]),
        sync_token=str(body["sync_token"]),
        store_name=str(body.get("store_name") or ""),
        cloud_base_url=str(body.get("cloud_base_url") or base_url),
    )


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #


def activate(
    code: str,
    *,
    database: Database,
    vault: SecretVault,
    transport: ActivationTransport,
    force: bool = False,
) -> ActivationResult:
    """Ativa o terminal e persiste a identidade recebida.

    Args:
        force: permite reapontar um terminal já ativado para outro tenant.
            Só use com a fila vazia e com decisão consciente do suporte.

    Raises:
        ActivationError: código inválido ou falha de comunicação.
        ActivationRefused: o servidor recusou o código.
        ActivationBlocked: a troca destruiria a rastreabilidade de vendas
            ainda não sincronizadas.
    """
    normalized = normalize_code(code)
    store = SettingsStore(database)
    current = store.load()

    result = transport.activate(
        normalized, machine_fingerprint(), vault.ensure_device_secret()
    )

    changing_tenant = (
        current.tenant_id is not None and current.tenant_id != result.tenant_id
    )
    if changing_tenant and not force:
        pending = OutboxReader(database).pending_count()
        if pending:
            raise ActivationBlocked(
                f"Este terminal tem {pending} registro(s) ainda não sincronizado(s) "
                f"com a loja atual. Sincronize antes de reapontá-lo para outra "
                f"loja — caso contrário essas vendas seriam enviadas para o CNPJ "
                f"errado."
            )

    # Token primeiro: gravar as configurações antes deixaria o terminal
    # "ativado" sem credencial se a escrita do cofre falhasse, e ele tentaria
    # sincronizar contra um 401 eterno.
    vault.store(SYNC_TOKEN_NAME, result.sync_token.encode("utf-8"))

    store.set_many(
        {
            "device.tenant_id": result.tenant_id,
            "device.store_id": result.store_id,
            "device.id": result.device_id,
            "device.activated": "1",
            "cloud.base_url": result.cloud_base_url,
        }
    )

    logger.info(
        "Terminal ativado: device=%s tenant=%s loja=%r",
        result.device_id,
        result.tenant_id,
        result.store_name,
    )
    return result


def is_activated(database: Database) -> bool:
    return SettingsStore(database).load().activated


def load_sync_token(vault: SecretVault) -> str | None:
    """Token de sincronização em texto, ou `None` se o terminal não foi ativado."""
    raw = vault.load(SYNC_TOKEN_NAME)
    return raw.decode("utf-8") if raw is not None else None
