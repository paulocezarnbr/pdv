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

import logging
from typing import TYPE_CHECKING, Any

from pdv.config import cloud_api_root
from pdv.remote.protocol import CommandKind, RemoteCommand
from pdv.sync.protocol import (
    AuthError,
    CommandDelivery,
    CommandFetch,
    CommandReport,
    ItemAck,
    ItemStatus,
    PullRequest,
    PullResponse,
    PushBatch,
    PushResponse,
    TerminalHealth,
    TransportError,
)

if TYPE_CHECKING:  # pragma: no cover
    import httpx

logger = logging.getLogger(__name__)


class HttpTransport:
    """Cliente HTTP do endpoint de sincronização."""

    def __init__(
        self,
        base_url: str,
        device_token: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        # As rotas abaixo são relativas à raiz `/api` (ver `cloud_api_root`).
        self._base_url = cloud_api_root(base_url)
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


    # -- comandos do painel --------------------------------------------------- #

    def fetch_commands(self, request: CommandFetch) -> CommandDelivery:
        """Busca comandos endereçados a este terminal.

        Um comando malformado na resposta é **descartado**, não fatal: uma
        entrada estranha no meio do lote não pode impedir que os outros
        comandos legítimos cheguem. O que ela perde é a chance de ser aplicada,
        e isso é o resultado certo — comando que não se consegue nem ler não se
        obedece.
        """
        client = self._get_client()

        try:
            response = client.get(
                "/commands/pending",
                params={
                    "tenant_id": request.tenant_id,
                    "store_id": request.store_id,
                    "device_id": request.device_id,
                    "limit": request.limit,
                },
            )
        except Exception as exc:
            raise TransportError(f"Falha de rede: {exc}") from exc

        body = self._body(response)
        commands = []
        for entry in body.get("commands", []):
            parsed = _parse_command(entry)
            if parsed is None:
                logger.warning("Comando ilegível descartado: %r", entry)
                continue
            commands.append(parsed)

        return CommandDelivery(commands=tuple(commands))

    def report_commands(self, report: CommandReport) -> tuple[str, ...]:
        client = self._get_client()

        try:
            response = client.post(
                "/commands/results",
                json={
                    "tenant_id": report.tenant_id,
                    "store_id": report.store_id,
                    "device_id": report.device_id,
                    "results": [
                        {
                            "command_uuid": result.command_uuid,
                            "status": result.status.value,
                            "message": result.message,
                            "settled_at": result.settled_at,
                        }
                        for result in report.results
                    ],
                    "awaiting": [
                        {
                            "command_uuid": notice.command_uuid,
                            "message": notice.message,
                            "requested_at": notice.requested_at,
                        }
                        for notice in report.awaiting
                    ],
                },
            )
        except Exception as exc:
            raise TransportError(f"Falha de rede: {exc}") from exc

        body = self._body(response)
        # Só o que a nuvem nomear sai da fila de relato. Uma resposta vazia
        # significa "não confirmei nada", e o terminal relata de novo.
        return tuple(str(uuid) for uuid in body.get("accepted", []))

    # -- interno -------------------------------------------------------------- #

    def heartbeat(self, health: TerminalHealth) -> int:
        """Relata a saúde da fila. Devolve o desvio do relógio calculado na nuvem."""
        client = self._get_client()
        try:
            response = client.post(
                "/devices/heartbeat",
                json={
                    "device_id": health.device_id,
                    "tenant_id": health.tenant_id,
                    "terminal_clock": health.terminal_clock,
                    "pending_items": health.pending_items,
                    "quarantined_items": health.quarantined_items,
                    "oldest_pending_at": health.oldest_pending_at,
                    "last_quarantine_reason": health.last_quarantine_reason,
                },
            )
        except Exception as exc:
            raise TransportError(f"Falha de rede: {exc}") from exc
        return int(self._body(response).get("clock_drift_ms") or 0)

    def _body(self, response: Any) -> dict[str, Any]:
        """Trata os códigos de erro do mesmo jeito em toda rota."""
        if response.status_code in (401, 403):
            raise AuthError(f"Terminal não autorizado (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise TransportError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            return dict(response.json())
        except Exception as exc:
            raise TransportError(f"Resposta ilegível do servidor: {exc}") from exc


def _parse_command(entry: dict[str, Any]) -> RemoteCommand | None:
    """Monta o comando sem conferir nada além da forma.

    Um `kind` que este terminal não conhece vira `None` em vez de exceção: a
    nuvem pode ser mais nova que o PDV, e o comportamento seguro diante de uma
    ordem que não se entende é não obedecer — nunca adivinhar.
    """
    try:
        return RemoteCommand(
            command_uuid=str(entry["command_uuid"]),
            tenant_id=str(entry["tenant_id"]),
            store_id=str(entry["store_id"]),
            device_id=str(entry["device_id"]),
            kind=CommandKind(str(entry["kind"])),
            payload=dict(entry.get("payload") or {}),
            issued_by_user_id=str(entry["issued_by_user_id"]),
            issued_by_name=str(entry.get("issued_by_name") or ""),
            issued_at=str(entry["issued_at"]),
            signature=str(entry["signature"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


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
