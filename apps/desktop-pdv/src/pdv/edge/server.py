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
from pdv.domain.errors import AuthorizationRequiredError, PdvError
from pdv.domain.models import EntityId
from pdv.edge.auth import DeviceAuthError, EdgeAuth, PairedDevice, PairingError
from pdv.edge.hub import EventHub
from pdv.edge.kds import InvalidTransitionError, KdsService, TicketNotFoundError
from pdv.edge.manager import ManagerSessions
from pdv.edge.orders import (
    OrderClosedError,
    OrderNotFoundError,
    ProductNotSellableError,
    TableOccupiedError,
    TableOrderService,
)
from pdv.edge.staff import StaffAuthError, StaffSession, StaffSessions
from pdv.edge.tables import TableError, TableService
from pdv.edge.webapp import WEBAPP_DIR, index_html
from pdv.services.authorization import AuthorizationService
from pdv.services.staff_report import StaffReport

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
        from fastapi.responses import HTMLResponse, Response
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover
        raise PdvError(
            "FastAPI não instalado — execute: pip install fastapi uvicorn[standard]"
        ) from exc

    event_hub = hub or EventHub()
    auth = EdgeAuth(database, config.tenant_id, config.store_id)
    orders = TableOrderService(database, config, event_hub)
    kds = KdsService(database, config, event_hub)
    tables = TableService(database, config)
    managers = ManagerSessions(AuthorizationService(database, config.tenant_id))
    staff = StaffSessions(database, config.tenant_id)
    staff_report = StaffReport(database, config)

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
        # `operator_id` **saiu** do corpo. Quem abre a comanda é a sessão que
        # assina a requisição, não um campo que o cliente preenche: enquanto
        # veio do corpo, o app mandava o id do próprio aparelho e qualquer
        # cliente podia lançar no nome de quem quisesse.
        #
        # Um dos dois abaixo. O `table_id` é o caminho normal (o app mostra o
        # mapa e o garçom toca na mesa); o rótulo fica aceito para aparelho
        # antigo ainda não atualizado, e é resolvido contra o cadastro do
        # mesmo jeito.
        table_id: str = Field(default="", max_length=64)
        table_label: str = Field(default="", max_length=32)

    class StaffLoginRequest(BaseModel):
        login: str = Field(min_length=1, max_length=64)
        pin: str = Field(min_length=1, max_length=64)

    class ReasonRequest(BaseModel):
        reason: str = Field(min_length=3, max_length=200)

    class TransferRequest(BaseModel):
        table_id: str = Field(min_length=1, max_length=64)

    class ManagerLoginRequest(BaseModel):
        login: str = Field(min_length=1, max_length=64)
        pin: str = Field(min_length=1, max_length=64)

    class TableRequest(BaseModel):
        label: str = Field(min_length=1, max_length=32)
        area: str = Field(default="Salão", min_length=1, max_length=32)
        seats: int = Field(default=4, ge=1, le=99)
        sort_order: int | None = Field(default=None, ge=0, le=9999)

    class TablePatchRequest(BaseModel):
        label: str | None = Field(default=None, min_length=1, max_length=32)
        area: str | None = Field(default=None, min_length=1, max_length=32)
        seats: int | None = Field(default=None, ge=1, le=99)
        sort_order: int | None = Field(default=None, ge=0, le=9999)
        is_active: bool | None = None

    class SeedTablesRequest(BaseModel):
        count: int = Field(default=12, ge=1, le=100)
        area: str = Field(default="Salão", min_length=1, max_length=32)

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

    def current_manager(
        device: Device,
        x_manager_token: Annotated[str | None, Header()] = None,
    ) -> Any:
        """A concessão de gerente, conferida contra **este** aparelho.

        Cabeçalho próprio, separado do `Authorization`: são duas identidades
        diferentes — o aparelho e a pessoa. Empilhar as duas no mesmo cabeçalho
        faria o app ter de esquecer a do aparelho para usar a da pessoa.
        """
        try:
            return managers.require(x_manager_token, EntityId(device.id))
        except AuthorizationRequiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
                # Duas credenciais diferentes recusam com o mesmo 403, e o app
                # precisa saber **qual** pedir de novo. Sem este cabeçalho ele
                # teria de adivinhar pelo texto da mensagem — que muda.
                headers={"X-Auth-Scope": "manager"},
            ) from exc

    Manager = Annotated[Any, Depends(current_manager)]

    def current_staff(
        device: Device,
        x_staff_token: Annotated[str | None, Header()] = None,
    ) -> StaffSession:
        """Quem está atendendo neste aparelho.

        Cabeçalho próprio, pelo mesmo motivo do gerente: o aparelho e a pessoa
        são identidades diferentes. O `Authorization` continua sendo do
        aparelho — é ele que o caixa revoga quando o celular some.

        Todas as rotas que **escrevem** na comanda passam por aqui. As de
        leitura (mapa, cardápio) não: um aparelho pareado pode olhar o salão
        antes de alguém entrar, e exigir sessão para ver o mapa daria uma tela
        de login em cima de uma tela vazia.
        """
        try:
            return staff.require(x_staff_token, EntityId(device.id))
        except StaffAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
                headers={"X-Auth-Scope": "staff"},
            ) from exc

    Staff = Annotated[StaffSession, Depends(current_staff)]

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

    def _conflict(exc: Exception) -> HTTPException:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    # -- rotas ------------------------------------------------------------- #

    @app.get("/", include_in_schema=False)
    async def waiter_app() -> Any:
        """O app do garçom. Servido pelo mesmo processo que tem a comanda.

        Sem loja de aplicativo e sem etapa de instalação: o garçom aponta a
        câmera para o QR do caixa e está dentro. Ver `webapp/__init__.py` para
        por que isto existe ao lado do app nativo, e não no lugar dele.
        """
        return HTMLResponse(index_html())

    @app.get("/vendor/{filename}", include_in_schema=False)
    async def vendor(filename: str) -> Any:
        """Bibliotecas de terceiros, servidas pelo próprio PDV.

        **Não** vêm de CDN, e o motivo é o de sempre: a loja opera sem
        internet. Uma tag apontando para jsdelivr transformaria "a internet
        caiu" em "o app do garçom não mostra mais nenhum aviso" — justamente no
        momento em que os avisos importam. O arquivo vem junto do PDV e
        atualiza com ele.

        Só nomes do diretório `vendor/`, resolvidos e conferidos contra ele: o
        `filename` vem da URL, e concatená-lo sem checar deixaria `../` ler
        qualquer arquivo da máquina do caixa.
        """
        target = (WEBAPP_DIR / "vendor" / filename).resolve()
        root = (WEBAPP_DIR / "vendor").resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Arquivo inexistente."
            )

        media = "text/css" if target.suffix == ".css" else "application/javascript"
        return Response(
            target.read_bytes(),
            media_type=media,
            # Versionado pelo nome do arquivo e trocado só no update do PDV:
            # cache longo economiza o Wi-Fi da loja a cada abertura do app.
            headers={"Cache-Control": "public, max-age=604800"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> Any:
        """Deixa o app ser fixado na tela inicial do celular.

        Um atalho na tela inicial abre em tela cheia, sem barra de endereço —
        que é a diferença entre "um site do caixa" e "o app do salão" para quem
        vai usar isto doze horas por dia.
        """
        return {
            "name": f"Salão — {config.store_name}",
            "short_name": "Salão",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0e1116",
            "theme_color": "#0e1116",
            "orientation": "portrait",
        }

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
    async def open_order(
        request: OpenOrderRequest, device: Device, session: Staff
    ) -> OrderResponse:
        try:
            order = orders.open_order(
                client_uuid=EntityId(request.client_uuid),
                operator_id=session.user_id,
                table_id=EntityId(request.table_id) if request.table_id else None,
                table_label=request.table_label,
                origin_device_id=device.id,
            )
        except TableOccupiedError as exc:
            # 409 com a comanda existente no corpo: o app abre essa em vez de
            # mostrar erro. Tocar numa mesa ocupada é querer lançar nela.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": str(exc), "order": exc.order.to_json()},
            ) from exc
        except TableError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except PdvError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        return _as_order(order)

    @app.get("/orders/{order_id}")
    async def order_detail(order_id: str, device: Device) -> dict[str, Any]:
        try:
            order = orders.get_order(EntityId(order_id))
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        return {**order.to_json(), "items": orders.list_items(EntityId(order_id))}

    @app.post("/orders/{order_id}/bill", response_model=OrderResponse)
    async def request_bill(
        order_id: str, device: Device, session: Staff
    ) -> OrderResponse:
        """Pedir a conta. Quem **recebe** é o caixa — ver `orders.request_bill`."""
        try:
            return _as_order(orders.request_bill(EntityId(order_id)))
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except OrderClosedError as exc:
            raise _conflict(exc) from exc

    @app.delete("/orders/{order_id}/bill", response_model=OrderResponse)
    async def clear_bill(
        order_id: str, device: Device, session: Staff
    ) -> OrderResponse:
        """A mesa desistiu de fechar e pediu sobremesa."""
        try:
            return _as_order(orders.clear_bill_request(EntityId(order_id)))
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc

    @app.post("/orders/{order_id}/cancel", response_model=OrderResponse)
    async def cancel_order(
        order_id: str, request: ReasonRequest, device: Device,
        session: Staff, manager: Manager,
    ) -> OrderResponse:
        """Cancelar a comanda inteira — **só com gerente**."""
        try:
            order = orders.cancel_order(
                order_id=EntityId(order_id),
                authorizer_id=EntityId(manager.id),
                authorizer_name=manager.name,
                reason=request.reason,
            )
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except PdvError as exc:
            raise _conflict(exc) from exc
        return _as_order(order)

    @app.post("/orders/{order_id}/transfer", response_model=OrderResponse)
    async def transfer_order(
        order_id: str, request: TransferRequest, device: Device,
        session: Staff, manager: Manager,
    ) -> OrderResponse:
        try:
            order = orders.transfer(
                order_id=EntityId(order_id),
                table_id=EntityId(request.table_id),
                authorizer_id=EntityId(manager.id),
                authorizer_name=manager.name,
            )
        except TableOccupiedError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": str(exc), "order": exc.order.to_json()},
            ) from exc
        except OrderNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except PdvError as exc:
            raise _conflict(exc) from exc
        return _as_order(order)

    @app.post("/orders/{order_id}/items", response_model=OrderResponse)
    async def add_item(
        order_id: str, request: AddItemRequest, device: Device, session: Staff
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
                created_by_user_id=session.user_id,
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

    # -- mesas -------------------------------------------------------------- #

    @app.get("/tables")
    async def list_tables(device: Device, include_inactive: bool = False) -> dict[str, Any]:
        """O mapa do salão com a ocupação de cada mesa.

        É a tela inicial do app: o garçom vê livre, ocupada e pedindo conta sem
        abrir nada.
        """
        return {
            "tables": [
                t.to_json()
                for t in tables.list_tables(include_inactive=include_inactive)
            ]
        }

    @app.post("/tables")
    async def create_table(
        request: TableRequest, device: Device, manager: Manager
    ) -> dict[str, Any]:
        try:
            table = tables.create(
                label=request.label,
                area=request.area,
                seats=request.seats,
                sort_order=request.sort_order,
            )
        except TableError as exc:
            raise _conflict(exc) from exc
        return table.to_json()

    @app.patch("/tables/{table_id}")
    async def patch_table(
        table_id: str, request: TablePatchRequest, device: Device, manager: Manager
    ) -> dict[str, Any]:
        # A ordem importa: `update` recusa mesa inativa (editar o que está fora
        # do mapa esconderia a alteração do gerente). Então reativar vem antes
        # da edição, e desativar vem depois — assim renomear e tirar do mapa na
        # mesma tela funciona nos dois sentidos.
        def edit() -> Any:
            return tables.update(
                EntityId(table_id),
                label=request.label,
                area=request.area,
                seats=request.seats,
                sort_order=request.sort_order,
            )

        try:
            if request.is_active is True:
                tables.set_active(EntityId(table_id), True)
                table = edit()
            elif request.is_active is False:
                edit()
                table = tables.set_active(EntityId(table_id), False)
            else:
                table = edit()
        except TableError as exc:
            raise _conflict(exc) from exc
        return table.to_json()

    @app.post("/tables/seed")
    async def seed_tables(
        request: SeedTablesRequest, device: Device, manager: Manager
    ) -> dict[str, Any]:
        """Cria "Mesa 1".."Mesa N" de uma vez, pulando o que já existe."""
        try:
            created = tables.seed_default_tables(request.count, area=request.area)
        except TableError as exc:
            raise _conflict(exc) from exc
        return {"created": created, "tables": [t.to_json() for t in tables.list_tables()]}

    # -- garçom -------------------------------------------------------------- #

    @app.post("/staff/session")
    async def staff_login(
        request: StaffLoginRequest, device: Device
    ) -> dict[str, Any]:
        """O garçom entra com a credencial dele, no aparelho já pareado.

        Sem sessão na entrada — é esta rota que a emite. Exige o token do
        **aparelho**: um PIN vazado não vale em celular de fora da loja, e um
        celular perdido não vale sem o PIN de alguém.
        """
        try:
            session = staff.login(
                login=request.login, pin=request.pin, device_id=EntityId(device.id)
            )
        except AuthorizationRequiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        return session.to_json(with_token=True)

    @app.get("/staff/session")
    async def staff_check(session: Staff) -> dict[str, Any]:
        """Quem está em turno neste aparelho. O app confirma ao abrir."""
        return session.to_json()

    @app.delete("/staff/session")
    async def staff_logout(
        device: Device,
        x_staff_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return {"ended": staff.logout(x_staff_token)}

    @app.get("/staff/summary")
    async def staff_summary(session: Staff) -> dict[str, Any]:
        """O resultado do próprio turno.

        Só os números de quem está autenticado — nunca os do colega. Ver quanto
        o outro fez de gorjeta não é informação de trabalho, é o começo de uma
        conversa que o gerente é quem tem de ter.
        """
        return staff_report.for_user(session.user_id)

    # -- gerente ------------------------------------------------------------ #

    @app.post("/manager/session")
    async def manager_login(
        request: ManagerLoginRequest, device: Device
    ) -> dict[str, Any]:
        """Autoriza um gerente neste aparelho, por poucos minutos.

        Sem token de gerente na entrada — é esta rota que o emite. O PIN é
        validado offline, contra a réplica local, com o mesmo bloqueio
        progressivo do balcão.
        """
        try:
            grant = managers.authorize(
                login=request.login, pin=request.pin, device_id=EntityId(device.id)
            )
        except AuthorizationRequiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        return grant.to_json()

    @app.delete("/manager/session")
    async def manager_logout(
        device: Device,
        x_manager_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return {"revoked": managers.revoke(x_manager_token)}

    @app.get("/manager/session")
    async def manager_check(manager: Manager) -> dict[str, Any]:
        """O app usa para saber se ainda pode mostrar as opções de gerente."""
        return {"user_id": manager.id, "name": manager.name, "role": manager.role}

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
