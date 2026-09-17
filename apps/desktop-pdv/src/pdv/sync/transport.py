"""Transporte HTTP até a nuvem.

Duas decisões que definem o comportamento sob falha:

1. **Timeout curto e sem retry interno.** A biblioteca HTTP não retenta: quem
   retenta é a fila, com backoff e idempotência. Retry dentro do transporte
   duplicaria a lógica de reenvio em dois lugares e tornaria impossível
   raciocinar sobre quantas vezes um lote realmente chegou ao servidor.

2. **Todo erro vira `TransportError`, que é sempre retentável.** A única
   exceção é 401/403, que vira `AuthError`. Um 500 pode ter sido gerado
   *depois* do commit; tratá-lo como falha definitiva perderia a venda.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pdv.sync.protocol import (
    AuthError,
    ItemAck,
    ItemStatus,
    PullRequest,
    PullResponse,
    PushBatch,
    PushResponse,
    TransportError,
)

if TYPE_CHECKING:  # pragma: no cover
    import httpx


class HttpTransport:
    """Cliente HTTP do endpoint de sincronização."""

    def __init__(
        self,
        base_url: str,
        device_token: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token
        self._timeout = timeout_seconds
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise TransportError(
                    "httpx não instalado — execute: pip install httpx"
                ) from exc

            self._client = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._device_token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- push ----------------------------------------------------------------- #

    def push(self, batch: PushBatch) -> PushResponse:
        client = self._get_client()

        payload = {
            "device_id": batch.device_id,
            "tenant_id": batch.tenant_id,
            "store_id": batch.store_id,
            "items": [
                {
                    "entity_table": item.entity_table,
                    "entity_id": item.entity_id,
                    "client_uuid": item.client_uuid,
                    "operation": item.operation,
                    "payload": item.payload,
                }
                for item in batch.items
            ],
        }

        try:
            response = client.post(
                "/sync/push",
                json=payload,
                # A chave de idempotência é derivada do CONTEÚDO do lote: uma
                # retentativa após timeout envia exatamente a mesma chave, e o
                # servidor reconhece a repetição mesmo tendo aplicado o primeiro
                # envio antes de a resposta se perder.
                headers={"Idempotency-Key": batch.idempotency_key},
            )
        except Exception as exc:  # httpx.TimeoutException, ConnectError, ...
            raise TransportError(f"Falha de rede: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(f"Terminal não autorizado (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise TransportError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            body: dict[str, Any] = response.json()
        except Exception as exc:
            raise TransportError(f"Resposta ilegível do servidor: {exc}") from exc

        return PushResponse(
            acks=tuple(
                ItemAck(
                    client_uuid=entry["client_uuid"],
                    status=_parse_status(entry.get("status")),
                    message=entry.get("message"),
                    server_seq=entry.get("server_seq"),
                )
                for entry in body.get("results", [])
            )
        )

    # -- pull ----------------------------------------------------------------- #

    def pull(self, request: PullRequest) -> PullResponse:
        client = self._get_client()

        try:
            response = client.get(
                "/sync/pull",
                params={
                    "tenant_id": request.tenant_id,
                    "store_id": request.store_id,
                    "entity_table": request.entity_table,
                    "since": request.since_server_seq,
                    "limit": request.limit,
                },
            )
        except Exception as exc:
            raise TransportError(f"Falha de rede: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError(f"Terminal não autorizado (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise TransportError(f"HTTP {response.status_code}")

        body = response.json()
        return PullResponse(
            entity_table=request.entity_table,
            rows=tuple(body.get("rows", [])),
            last_server_seq=int(body.get("last_server_seq", request.since_server_seq)),
            has_more=bool(body.get("has_more", False)),
        )


def _parse_status(value: str | None) -> ItemStatus:
    """Status desconhecido vira REJECTED, nunca sucesso.

    Se um servidor mais novo inventar um status que este cliente não conhece,
    o comportamento seguro é **não** marcar como sincronizado. Otimismo aqui
    significaria apagar da fila um dado que talvez não tenha sido gravado.
    """
    try:
        return ItemStatus(value)
    except ValueError:
        return ItemStatus.REJECTED
