"""O servidor local rodando ao lado da UI do caixa.

Uvicorn quer ser dono do laço de eventos e do tratamento de sinais; a UI do Qt
quer ser dona da thread principal. Os dois não cabem no mesmo lugar, então o
servidor vive numa thread própria com seu próprio laço asyncio.

`install_signal_handlers=False` é obrigatório: só a thread principal pode
registrar handler de sinal, e sem isso o servidor derruba o processo inteiro na
primeira tentativa — levando junto a venda em andamento.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.edge.discovery import ServiceAnnouncer, ServiceInfoData
from pdv.edge.hub import EventHub
from pdv.edge.server import DEFAULT_PORT, create_app
from pdv.edge.tls import TlsMaterial, default_hosts, ensure_certificate

logger = logging.getLogger(__name__)


class EdgeServer:
    """Ciclo de vida do servidor local e do anúncio mDNS."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        port: int = DEFAULT_PORT,
        hub: EventHub | None = None,
        announce: bool = True,
    ) -> None:
        self._database = database
        self._config = config
        self._port = port
        self._hub = hub or EventHub()
        self._announce = announce

        self._thread: threading.Thread | None = None
        self._server = None
        self._announcer: ServiceAnnouncer | None = None
        self._ready = threading.Event()
        self._tls: TlsMaterial | None = None

    @property
    def hub(self) -> EventHub:
        return self._hub

    @property
    def port(self) -> int:
        return self._port

    @property
    def tls(self) -> TlsMaterial | None:
        """O certificado em uso, ou `None` se o salão subiu em claro."""
        return self._tls

    @property
    def scheme(self) -> str:
        """`https` ou `http` — o que o painel do caixa dita para o celular."""
        return "https" if self._tls is not None else "http"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10.0) -> bool:
        """Sobe o servidor. Devolve se ficou pronto dentro do tempo.

        Uma porta ocupada (outro PDV na mesma máquina, ou o app aberto duas
        vezes) não pode impedir a venda no balcão: o erro é registrado, o
        salão fica sem servidor e o caixa segue funcionando.
        """
        if self.is_running:
            return True

        try:
            import uvicorn
        except ImportError:  # pragma: no cover
            logger.error("uvicorn ausente: servidor local indisponível")
            return False

        self._tls = self._build_tls()

        app = create_app(self._database, self._config, self._hub)
        server_config = uvicorn.Config(
            app,
            # 0.0.0.0 para aceitar os celulares da LAN. Quem autentica é o token
            # do aparelho, não a origem do pacote — ver auth.py.
            host="0.0.0.0",  # noqa: S104
            port=self._port,
            log_level="warning",
            access_log=False,
            ssl_certfile=str(self._tls.certificate_path) if self._tls else None,
            ssl_keyfile=str(self._tls.key_path) if self._tls else None,
        )
        self._server = uvicorn.Server(server_config)
        self._server.install_signal_handlers = False

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="pdv-edge", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            logger.error("Servidor local não subiu em %.0fs", timeout)
            return False

        if self._announce:
            self._announcer = ServiceAnnouncer(
                ServiceInfoData(
                    store_id=self._config.store_id,
                    store_name=self._config.store_name,
                    device_id=self._config.device_id,
                    port=self._port,
                    scheme=self.scheme,
                )
            )
            self._announcer.start()

        logger.info(
            "Servidor local ouvindo em %s na porta %d", self.scheme, self._port
        )
        return True

    def _build_tls(self) -> TlsMaterial | None:
        """Prepara o certificado, salvo se a loja tiver pedido para não usar.

        `PDV_EDGE_TLS=0` existe para diagnóstico — capturar o tráfego com um
        analisador de rede quando o app do garçom não conecta — e não para uso
        normal. Por isso o aviso é explícito no log: quem desligar isto e
        esquecer deixa o token do aparelho e o PIN em claro na rede da loja.
        """
        if os.getenv("PDV_EDGE_TLS", "1") == "0":
            logger.warning(
                "PDV_EDGE_TLS=0: o salão sobe em HTTP. "
                "Token de aparelho e PIN trafegam em claro na rede da loja."
            )
            return None

        return ensure_certificate(
            self._config.database_path.parent / "tls",
            store_name=self._config.store_name,
            hosts=default_hosts(),
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self._announcer is not None:
            self._announcer.stop()
            self._announcer = None

        if self._server is not None:
            self._server.should_exit = True

        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

        self._server = None
        self._tls = None
        logger.info("Servidor local encerrado")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.create_task(self._signal_ready())
            loop.run_until_complete(self._server.serve())  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            logger.exception("Servidor local encerrou com erro")
        finally:
            # Destrava quem espera em start(): sem isto, uma falha na subida
            # deixaria a UI parada no timeout inteiro.
            self._ready.set()
            loop.close()

    async def _signal_ready(self) -> None:
        while self._server is not None and not getattr(self._server, "started", False):
            await asyncio.sleep(0.02)
        self._ready.set()
