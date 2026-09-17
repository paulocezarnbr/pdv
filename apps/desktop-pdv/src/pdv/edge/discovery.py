"""Anúncio do PDV na LAN via mDNS (`_pdvedge._tcp`).

O problema que isto resolve é humano, não técnico: sem descoberta, instalar o
app do garçom significa alguém digitar `192.168.0.14:8420` em cada celular — e
refazer isso toda vez que o roteador reiniciar e o DHCP trocar o endereço. Com
mDNS o celular acha o PDV sozinho, inclusive depois da troca de IP.

Limites honestos
----------------

**mDNS não atravessa VLAN nem isolamento de cliente.** Roteador com "AP/client
isolation" ligado — comum em equipamento de provedor — bloqueia o tráfego entre
aparelhos da mesma rede e nenhuma descoberta funciona. Por isso o app precisa
manter a opção de endereço manual: quando a rede da loja é hostil, digitar o IP
é a saída, e escondê-la transformaria um contratempo em chamado.

**Descobrir não é autenticar.** O anúncio diz apenas onde o PDV está. Quem se
conecta ainda precisa do token de pareamento (`auth.py`).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_pdvedge._tcp.local."


@dataclass(frozen=True, slots=True)
class ServiceInfoData:
    store_id: str
    store_name: str
    device_id: str
    port: int


def local_ip_address() -> str:
    """IP desta máquina na LAN.

    O truque do socket UDP não envia pacote nenhum: só pede à pilha de rede qual
    interface ela usaria para sair. É mais confiável que `gethostbyname` numa
    máquina com várias placas (Wi-Fi + cabo + adaptador virtual do Docker), onde
    a resolução pelo nome frequentemente devolve a interface errada.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        # Máquina sem rota para fora (loja com internet caída) ainda tem LAN.
        return "127.0.0.1"
    finally:
        sock.close()


class ServiceAnnouncer:
    """Publica e retira o anúncio mDNS do terminal."""

    def __init__(self, info: ServiceInfoData) -> None:
        self._info = info
        self._zeroconf = None
        self._service = None

    def start(self) -> bool:
        """Anuncia. Devolve `False` se não foi possível — nunca levanta.

        Falhar aqui **não pode** impedir o PDV de vender: a descoberta é
        conveniência, a venda é o negócio. Sem anúncio, o app do garçom ainda
        chega pelo endereço manual.
        """
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("zeroconf ausente: PDV não será descoberto na LAN")
            return False

        address = local_ip_address()
        try:
            self._zeroconf = Zeroconf()
            self._service = ServiceInfo(
                SERVICE_TYPE,
                f"{self._info.store_name[:32]}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(address)],
                port=self._info.port,
                properties={
                    "store_id": self._info.store_id,
                    "store_name": self._info.store_name,
                    "device_id": self._info.device_id,
                    "version": "1.0.0",
                },
                server=f"pdv-{self._info.device_id[:8]}.local.",
            )
            self._zeroconf.register_service(self._service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Não foi possível anunciar na LAN: %s", exc)
            self.stop()
            return False

        logger.info("PDV anunciado em %s:%d via mDNS", address, self._info.port)
        return True

    def stop(self) -> None:
        """Retira o anúncio. Sem isto, o app persegue um PDV que já fechou."""
        try:
            if self._zeroconf is not None and self._service is not None:
                self._zeroconf.unregister_service(self._service)
        except Exception as exc:  # noqa: BLE001 - encerrando, não há a quem avisar
            logger.debug("Falha ao retirar o anúncio: %s", exc)
        finally:
            if self._zeroconf is not None:
                self._zeroconf.close()
            self._zeroconf = None
            self._service = None
