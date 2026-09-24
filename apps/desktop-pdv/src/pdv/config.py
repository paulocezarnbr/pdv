"""Configuração do terminal de PDV.

Tudo que varia de loja para loja (porta COM, protocolo da balança, nome da
impressora) vive aqui e é persistido em `device_settings`. Nada de constante
mágica espalhada pelo código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ScaleProtocolName = Literal["toledo_prix3", "filizola", "urano", "simulated"]
PrinterBackendName = Literal["win32raw", "escpos_usb", "file"]


@dataclass(frozen=True, slots=True)
class ScaleConfig:
    """Parâmetros da balança de checkout.

    Os *defaults* cobrem o caso mais comum no Brasil (9600 8N1). Modelos com
    firmware antigo podem exigir 8N2 ou paridade par — confira o manual antes
    de culpar o software.
    """

    protocol: ScaleProtocolName = "simulated"
    port: str = "COM3"
    baudrate: int = 9600
    bytesize: int = 8
    parity: Literal["N", "E", "O"] = "N"
    stopbits: float = 1
    timeout_seconds: float = 0.4
    poll_interval_seconds: float = 0.2
    stable_readings: int = 3
    """Leituras idênticas consecutivas para considerar o peso estável."""
    max_weight_grams: int = 30_000
    """Capacidade do equipamento; acima disso tratamos como sobrecarga."""


@dataclass(frozen=True, slots=True)
class PrinterConfig:
    """Parâmetros da impressora térmica Epson TM-T20X (80 mm)."""

    backend: PrinterBackendName = "file"
    windows_printer_name: str = "EPSON TM-T20X Receipt"
    """Nome exato como aparece em Dispositivos e Impressoras."""
    usb_vendor_id: int = 0x04B8
    """Epson."""
    usb_product_id: int = 0x0E28
    """TM-T20X — confirme com `lsusb` / Gerenciador de Dispositivos."""
    columns: int = 48
    """Fonte A em 80 mm."""
    codepage: int = 2
    """ESC t n → 2 = PC850 Multilingual (acentuação pt-BR)."""
    cut_feed_lines: int = 4
    open_drawer_on_cash: bool = True
    output_dir: Path = field(default_factory=lambda: Path("./out"))
    """Usado apenas pelo backend `file` (desenvolvimento e testes)."""


def cloud_api_root(base_url: str) -> str:
    """A raiz `/api` da nuvem, aceite quem digitou a origem ou a raiz.

    Quem configura o terminal digita o endereço que aparece no navegador —
    `https://api.loja.com.br` —, e todas as rotas da nuvem moram sob `/api`.
    A ativação e a sincronização colavam o caminho direto na origem e recebiam
    404; só o cliente fiscal normalizava. Agora os três passam por aqui.
    """
    base = base_url.rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"


@dataclass(frozen=True, slots=True)
class StockConfig:
    block_sale_on_negative_stock: bool = False
    """Padrão do food service: avisa, mas não trava a fila do caixa."""
    warn_threshold_percent: int = 10


@dataclass(frozen=True, slots=True)
class AppConfig:
    tenant_id: str
    store_id: str
    device_id: str
    device_secret: bytes = b"CHAVE-DE-DESENVOLVIMENTO-NAO-USE-EM-PRODUCAO"
    """Chave HMAC do ledger de auditoria.

    Em producao **nunca** fica em codigo nem no banco: e provisionada na
    ativacao do terminal e guardada via DPAPI do Windows, vinculada a maquina
    e a conta. Ainda assim, um administrador com depurador consegue extrai-la
    da memoria — por isso a garantia forte e a ancoragem no servidor, nao esta
    chave. Ver o cabecalho de services/audit.py.
    """
    store_name: str = "Confeitaria Demo"
    store_document: str = "00.000.000/0001-00"
    store_address: str = "Rua Exemplo, 123 - Centro"
    database_path: Path = field(default_factory=lambda: Path("./pdv_local.db"))
    cloud_base_url: str = "https://api.erpfood.local"
    scale: ScaleConfig = field(default_factory=ScaleConfig)
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    stock: StockConfig = field(default_factory=StockConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        """Carrega do ambiente. Em produção o instalador grava estes valores."""
        database_path = Path(os.getenv("PDV_DB_PATH", "./pdv_local.db"))
        return cls(
            tenant_id=os.getenv("PDV_TENANT_ID", "11111111-1111-1111-1111-111111111111"),
            store_id=os.getenv("PDV_STORE_ID", "22222222-2222-2222-2222-222222222222"),
            device_id=os.getenv("PDV_DEVICE_ID", "33333333-3333-3333-3333-333333333333"),
            device_secret=os.getenv(
                "PDV_DEVICE_SECRET", "CHAVE-DE-DESENVOLVIMENTO-NAO-USE-EM-PRODUCAO"
            ).encode("utf-8"),
            store_name=os.getenv("PDV_STORE_NAME", "Confeitaria Demo"),
            database_path=database_path,
            cloud_base_url=os.getenv("PDV_CLOUD_URL", "https://api.erpfood.local"),
            scale=ScaleConfig(
                protocol=os.getenv("PDV_SCALE_PROTOCOL", "simulated"),  # type: ignore[arg-type]
                port=os.getenv("PDV_SCALE_PORT", "COM3"),
                baudrate=int(os.getenv("PDV_SCALE_BAUD", "9600")),
            ),
            printer=PrinterConfig(
                backend=os.getenv("PDV_PRINTER_BACKEND", "file"),  # type: ignore[arg-type]
                windows_printer_name=os.getenv(
                    "PDV_PRINTER_NAME", "EPSON TM-T20X Receipt"
                ),
                # Ao lado do banco, nunca relativo ao diretório de trabalho:
                # instalado em Program Files o cwd é somente-leitura para o
                # operador, e o cupom do backend `file` falharia por permissão
                # — um erro de instalação disfarçado de erro de impressora.
                output_dir=Path(
                    os.getenv("PDV_RECEIPT_DIR", str(database_path.parent / "cupons"))
                ),
            ),
        )
