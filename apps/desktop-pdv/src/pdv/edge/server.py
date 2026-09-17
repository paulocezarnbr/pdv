"""API local do PDV — o terminal como servidor da loja.

Enquanto a internet estiver fora, **este** processo é a autoridade da loja: é
ele que numera pedidos, guarda a comanda e alimenta a cozinha. O celular do
garçom é cliente dele, não da nuvem.

Duas decisões que moldam o resto
--------------------------------

**Escuta em `0.0.0.0`, autentica sempre.** Precisa aceitar conexão dos celulares
da LAN, e a LAN é a mesma rede do Wi-Fi do cliente. Nenhuma rota de negócio
confia no IP de origem — o que vale é o token do aparelho, emitido por
pareamento presencial (ver `auth.py`).

**O WebSocket do KDS só empurra fato consumado.** Ele nunca aceita comando: a
tela da cozinha avança ticket por `POST`, que passa pela validação de transição.
Um WebSocket que aceita escrita vira o caminho sem autenticação por onde a
próxima versão do app derruba o estado da loja.

Por que o `client_uuid` vem do celular
--------------------------------------

Ver o cabeçalho de `orders.py`: é o que impede a mesma comanda de ser faturada
duas vezes quando o app alterna entre a rota LAN e a rota nuvem.
"""

#
# ATENÇÃO: este módulo **não** usa `from __future__ import annotations`, e é de
# propósito.
#
# Com ele, toda anotação vira string e o FastAPI precisa resolvê-la por
# `get_type_hints`, que só enxerga o escopo do módulo. Como os modelos e as
# dependências daqui nascem **dentro** de `create_app` (precisam do `config` e
# do banco no fecho), os nomes não existem lá fora: o FastAPI desiste do tipo e
# rebaixa cada parâmetro a query string. O sintoma é traiçoeiro — o servidor
# sobe, as rotas existem e toda requisição responde 422 reclamando de um
# parâmetro de query que ninguém declarou.

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.domain.errors import PdvError
from pdv.domain.models import EntityId
from pdv.edge.auth import DeviceAuthError, EdgeAuth, PairedDevice, PairingError
from pdv.edge.hub import EventHub
from pdv.edge.kds import InvalidTransitionError, KdsService, TicketNotFoundError
from pdv.edge.orders import (
    OrderClosedError,
    OrderNotFoundError,
    ProductNotSellableError,
    TableOrderService,
)

logger = logging.getLogger(__name__)

#: Porta do servidor local. Liberada no firewall pelo instalador, só no perfil
#: privado — expor o PDV na rede pública de um shopping seria outro problema.
DEFAULT_PORT = 8420

#: Silêncio antes de mandar um ping no WebSocket. Sem isso, um roteador doméstico
#: derruba a conexão ociosa e a tela da cozinha congela mostrando dados velhos —
#: pior que mostrar erro, porque ninguém percebe.
WS_PING_SECONDS = 20.0

#: Espera máxima de cada consulta à fila do hub.
#:
#: A espera acontece numa thread do executor, e essa thread fica **presa** até
#: ela acabar — mesmo depois de o tablet já ter desconectado. Esperar os 20 s do
#: ping de uma vez seguraria uma thread por tela caída durante todo esse tempo;
#: uma cozinha com Wi-Fi ruim, reconectando em ciclo, esgotaria o executor e
#: travaria os aparelhos que ainda estão de pé. Fatiar a espera limita o
#: prejuízo a meio segundo por desconexão.
WS_POLL_SECONDS = 0.5


def create_app(
    database: Database,
    config: AppConfig,
    hub: EventHub | None = None,
) -> Any:
    """Monta a aplicação FastAPI do servidor local."""
    try:
        from fastapi import (
            Depends,
            FastAPI,
            Header,
            HTTPException,
            WebSocket,
            WebSocketDisconnect,
            status,
        )
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise PdvError(
            "FastAPI não instalado — execute: pip install fastapi uvicorn[standard]"
        ) from exc

    event_hub = hub or EventHub()
    auth = EdgeAuth(database, config.tenant_id, config.store_id)
    orders = TableOrderService(database, config, event_hub)
    kds = KdsService(database, config, event_hub)

    app = FastAPI(
        title="PDV Balcão — servidor local",
        version="1.0.0",
        # Documentação interativa desligada: é superfície a mais num processo
        # que roda no caixa da loja, e o app do garçom não a consome.
        docs_url=None,
        redoc_url=None,
    )

    # -- modelos ---------------------------------------------------------- #

    class PairRequest(BaseModel):
        code: str = Field(min_length=4, max_length=16)
        device_name: str = Field(min_length=1, max_length=64)
        kind: str = Field(default="waiter", pattern="^(waiter|kds)$")

    class PairResponse(BaseModel):
        device_id: str
        token: str
        store_name: str

    class OpenOrderRequest(BaseModel):
        client_uuid: str = Field(min_length=8, max_length=64)
        table_label: str = Field(min_length=1, max_length=32)
        operator_id: str = Field(min_length=1, max_length=64)

    class AddItemRequest(BaseModel):
        client_uuid: str = Field(min_length=8, max_length=64)
        product_id: str = Field(min_length=1, max_length=64)
        quantity: str = Field(default="1", max_length=12)
        notes: str = Field(default="", max_length=200)
        station: str = Field(default="cozinha", max_length=32)

    class OrderResponse(BaseModel):
        order_id: str
        client_uuid: str
        local_number: int
        table_label: str
        status: str
        total_cents: int
        item_count: int

    # -- dependências ----------------------------------------------------- #

    def current_device(
        authorization: Annotated[str | None, Header()] = None,
    ) -> PairedDevice:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:]
        try:
            return auth.authenticate(token)
        except DeviceAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc

    Device = Annotated[PairedDevice, Depends(current_device)]

    def _as_order(order: Any) -> OrderResponse:
        return OrderResponse(
            order_id=order.id,
            client_uuid=order.client_uuid,
            local_number=order.local_number,
            table_label=order.table_label,
            status=order.status,
            total_cents=int(order.total_cents),
            item_count=order.item_count,
        )

    # -- rotas ------------------------------------------------------------- #

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Aberta de propósito: é por ela que o app confirma que achou o PDV.

        Não revela nada além de que existe um PDV aqui e qual loja ele atende —
        informação que qualquer aparelho pareado já teria.
        """
        return {
            "service": "pdv-edge",
            "store_id": config.store_id,
            "store_name": config.store_name,
            "device_id": config.device_id,
            "version": "1.0.0",
        }

    @app.post("/pair", response_model=PairResponse)
    async def pair(request: PairRequest) -> PairResponse:
        """Sem token: é justamente esta rota que emite o primeiro."""
        try:
            token = auth.pair(
                request.code, device_name=request.device_name, kind=request.kind
            )
        except PairingError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc

        device = auth.authenticate(token)
        return PairResponse(
            device_id=device.id, token=token, store_name=config.store_name
        )

    @app.get("/menu")
    async def menu(device: Device) -> dict[str, Any]:
        products = ProductRepository(database.connection).list_active(
            EntityId(config.tenant_id)
        )
        return {
            "products": [
                {
                    "id": p.id,
                    "sku": p.sku,
                    "name": p.name,
                    "price_cents": int(p.price_cents),
                    "pricing_mode": p.pricing_mode.value,
                    # O app esconde o que ele não pode vender em vez de deixar o
                    # garçom descobrir no erro, com o cliente esperando.
                    "sellable_by_waiter": not p.is_weighed,
                }
                for p in products
            ]
        }

    @app.get("/orders", response_model=list[OrderResponse])
    async def list_orders(device: Device) -> list[OrderResponse]:
        return [_as_order(o) for o in orders.list_open_orders()]

    @app.post("/orders", response_model=OrderResponse)
    async def open_order(request: OpenOrderRequest, device: Device) -> OrderResponse:
        try:
            order = orders.open_order(
                client_uuid=EntityId(request.client_uuid),
                operator_id=EntityId(request.operator_id),
                table_label=request.table_label,
                origin_device_id=device.id,
            )
        except PdvError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return _as_order(order)

    @app.post("/orders/{order_id}/items", response_model=OrderResponse)
    async def add_item(
        order_id: str, request: AddItemRequest, device: Device
    ) -> OrderResponse:
        try:
            quantity = Decimal(request.quantity)
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Quantidade inválida: {request.quantity!r}",
            ) from exc

        try:
            order = orders.add_item(
                order_id=EntityId(order_id),
                client_uuid=EntityId(request.client_uuid),
                product_id=EntityId(request.product_id),
                quantity=quantity,
                notes=request.notes,
                station=request.station,
            )
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except (OrderClosedError, ProductNotSellableError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return _as_order(order)

    @app.get("/kds/tickets")
    async def kds_tickets(device: Device, station: str | None = None) -> dict[str, Any]:
        return {"tickets": [t.to_json() for t in kds.list_active(station)]}

    @app.post("/kds/tickets/{ticket_id}/{action}")
    async def kds_action(ticket_id: str, action: str, device: Device) -> dict[str, Any]:
        if action not in ("bump", "recall"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Ação desconhecida."
            )
        try:
            ticket = (
                kds.bump(EntityId(ticket_id))
                if action == "bump"
                else kds.recall(EntityId(ticket_id))
            )
        except TicketNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except InvalidTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return ticket.to_json()

    @app.websocket("/kds/stream")
    async def kds_stream(websocket: WebSocket) -> None:
        """Empurra os eventos da cozinha. Só leitura.

        O token vem na query porque a API de WebSocket do navegador não deixa
        definir cabeçalho no handshake. Fica no log do servidor, então é um
        token de aparelho (revogável do caixa), nunca uma senha de pessoa.
        """
        token = websocket.query_params.get("token")
        try:
            device = auth.authenticate(token)
        except DeviceAuthError:
            await websocket.close(code=4401, reason="aparelho não autenticado")
            return

        await websocket.accept()
        logger.info("KDS conectado: %s", device.name)

        subscription = event_hub.subscribe({"ticket.queued", "ticket.changed"})
        loop = asyncio.get_running_loop()
        try:
            # Estado completo primeiro: a tela que acabou de conectar (ou
            # reconectar após queda) precisa da fila inteira, não só do que
            # mudar daqui em diante.
            await websocket.send_json(
                {
                    "kind": "snapshot",
                    "tickets": [t.to_json() for t in kds.list_active()],
                }
            )

            silent_for = 0.0
            while True:
                # A fila do hub é bloqueante e vive noutra thread; esperar por
                # ela no executor mantém o laço de eventos livre para os outros
                # aparelhos conectados.
                event = await loop.run_in_executor(
                    None, subscription.get, WS_POLL_SECONDS
                )

                if event is None:
                    silent_for += WS_POLL_SECONDS
                    if silent_for < WS_PING_SECONDS:
                        continue
                    silent_for = 0.0
                    await websocket.send_json({"kind": "ping"})
                    continue

                silent_for = 0.0
                await websocket.send_json(event.to_json())
        except WebSocketDisconnect:
            logger.info("KDS desconectado: %s", device.name)
        except Exception:  # noqa: BLE001 - uma tela caindo não derruba o servidor
            logger.exception("Erro no stream do KDS")
        finally:
            subscription.close()

    return app
