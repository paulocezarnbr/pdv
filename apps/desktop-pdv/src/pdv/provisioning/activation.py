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

O que **nunca** trafega
-----------------------

O `device_secret` (chave HMAC do ledger) é gerado localmente e não sai da
máquina — ver `secrets.py`. A ativação transporta identidade e autorização, não
a chave que torna a auditoria verificável.

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

    def activate(self, code: str, fingerprint: dict[str, str]) -> ActivationResult: ...


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


#: O endereço de exemplo do `AppConfig`. Nunca é uma retaguarda de verdade: um
#: terminal instalado que ainda o carrega nunca conseguiria ativar, e por muito
#: tempo foi exatamente isso que o instalador tentava.
PLACEHOLDER_CLOUD_URL = "https://api.erpfood.local"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalize_server_url(raw: str) -> str:
    """O endereço da retaguarda como o terminal usa: `https://host[/...]/api`.

    O lojista digita o que o painel mostra na barra do navegador —
    `painel.minhaloja.com.br`, com ou sem `https://`, com ou sem barra no fim.
    As rotas do terminal vivem sob `/api`, então o sufixo é acrescentado aqui e
    não exigido de quem digita.

    HTTPS é obrigatório fora desta própria máquina: é por este endereço que o
    token de sincronização e todas as vendas vão trafegar. `http://` só para
    `localhost`, que é o caso de teste com a retaguarda no mesmo computador.

    Raises:
        ActivationError: endereço vazio, malformado ou sem HTTPS.
    """
    from urllib.parse import urlsplit, urlunsplit

    text = raw.strip()
    if not text:
        raise ActivationError("Informe o endereço da retaguarda.")
    if "://" not in text:
        text = "https://" + text

    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or " " in text:
        raise ActivationError(f"Endereço inválido: {raw.strip()!r}.")
    if parts.username or parts.password:
        raise ActivationError("O endereço não pode conter usuário ou senha.")
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ActivationError(
            "Use https:// — por este endereço vão passar o token do terminal e "
            "as vendas. http:// só é aceito para localhost."
        )

    path = parts.path.rstrip("/")
    if not path.endswith("/api"):
        path += "/api"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def display_server_url(api_url: str) -> str:
    """O endereço como o lojista o reconhece: sem o `/api` do final."""
    if not api_url or api_url == PLACEHOLDER_CLOUD_URL:
        return ""
    return api_url[: -len("/api")] if api_url.endswith("/api") else api_url


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
    """Fala com `POST {cloud}/devices/activate`."""

    def __init__(self, base_url: str, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def activate(self, code: str, fingerprint: dict[str, str]) -> ActivationResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ActivationError(
                "httpx não instalado — execute: pip install httpx"
            ) from exc

        try:
            response = httpx.post(
                f"{self._base_url}/devices/activate",
                json={"activation_code": code, "fingerprint": fingerprint},
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

    result = transport.activate(normalized, machine_fingerprint())

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
            # O nome aparece no login, no título da janela e no cupom. Sem
            # gravá-lo, um terminal ativado seguia dizendo "Confeitaria Demo".
            **({"store.name": result.store_name} if result.store_name else {}),
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
