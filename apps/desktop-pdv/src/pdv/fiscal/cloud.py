"""Emissão server-first com contingência local estritamente controlada.

Somente uma falha anterior ao estabelecimento da conexão autoriza a série
local de contingência. Timeout, HTTP 500 ou resposta ilegível são ambíguos: a
SEFAZ pode ter autorizado antes de a resposta se perder, portanto emitir de
novo localmente criaria duplicidade fiscal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pdv.domain.models import EntityId
from pdv.fiscal.service import FiscalDocument, FiscalService


class FiscalCloudError(RuntimeError):
    pass


class CloudDefinitelyOffline(FiscalCloudError):
    """Não houve conexão TCP; é seguro entrar em contingência local."""


class CloudResultUnknown(FiscalCloudError):
    """A requisição pode ter sido processada; nunca emitir outra nota."""


class CloudFiscalAuthError(FiscalCloudError):
    pass


@dataclass(frozen=True, slots=True)
class CloudFiscalDocument:
    request_uuid: str
    order_id: str
    status: Literal["processing", "unknown", "authorized", "rejected", "canceled"]
    series: int
    number: int
    access_key: str | None = None
    protocol: str | None = None
    reason: str | None = None


class CloudIssuer(Protocol):
    def issue(self, *, request_uuid: str, order_id: str) -> CloudFiscalDocument: ...
    def status(self, request_uuid: str) -> CloudFiscalDocument: ...


class HttpCloudFiscalGateway:
    def __init__(self, base_url: str, device_token: str, timeout_seconds: float = 25.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = device_token
        self._timeout = timeout_seconds
        self._client: Any | None = None

    def issue(self, *, request_uuid: str, order_id: str) -> CloudFiscalDocument:
        return self._call("POST", self._endpoint("fiscal/issue"), json={
            "request_uuid": request_uuid, "order_id": order_id,
        })

    def status(self, request_uuid: str) -> CloudFiscalDocument:
        return self._call("GET", self._endpoint("fiscal/status"),
                          params={"request_uuid": request_uuid})

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _endpoint(self, route: str) -> str:
        """Aceita tanto a origem (`https://host`) quanto a raiz `/api`."""
        prefix = self._base_url if self._base_url.endswith("/api") else f"{self._base_url}/api"
        return f"{prefix}/{route.lstrip('/')}"

    def _call(self, method: str, path: str, **kwargs: object) -> CloudFiscalDocument:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise FiscalCloudError("httpx não instalado") from exc
        if self._client is None:
            self._client = httpx.Client(
                base_url=self._base_url, timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise CloudDefinitelyOffline("Nuvem fiscal inacessível antes do envio.") from exc
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise CloudResultUnknown(
                "Resposta fiscal ambígua; consulte o status antes de qualquer contingência."
            ) from exc
        if response.status_code in (401, 403):
            raise CloudFiscalAuthError("Terminal não autorizado na nuvem fiscal.")
        if response.status_code >= 500:
            raise CloudResultUnknown("Servidor falhou após receber a solicitação fiscal.")
        if response.status_code >= 400:
            raise FiscalCloudError(f"Emissão fiscal recusada (HTTP {response.status_code}).")
        try:
            body = response.json()
            return CloudFiscalDocument(
                request_uuid=str(body["request_uuid"]), order_id=str(body["order_id"]),
                status=str(body["status"]),  # type: ignore[arg-type]
                series=int(body["series"]), number=int(body["number"]),
                access_key=body.get("access_key"), protocol=body.get("protocol"),
                reason=body.get("provider_reason"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudResultUnknown("Resposta fiscal ilegível; situação desconhecida.") from exc


@dataclass(frozen=True, slots=True)
class IssueDecision:
    mode: Literal["cloud", "local_contingency", "unknown"]
    cloud: CloudFiscalDocument | None = None
    local: FiscalDocument | None = None
    message: str = ""


class FiscalCoordinator:
    def __init__(self, cloud: CloudIssuer, local: FiscalService) -> None:
        self._cloud = cloud
        self._local = local

    def issue(self, *, request_uuid: str, order_id: EntityId) -> IssueDecision:
        try:
            document = self._cloud.issue(request_uuid=request_uuid, order_id=str(order_id))
            return IssueDecision("cloud", cloud=document)
        except CloudDefinitelyOffline:
            local = self._local.reserve(
                order_id=order_id, online=False,
                contingency_reason="nuvem comprovadamente inacessível antes do envio",
            )
            return IssueDecision("local_contingency", local=local)
        except CloudResultUnknown as exc:
            return IssueDecision("unknown", message=str(exc))
