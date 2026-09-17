"""Detecção automática de balança e impressora.

O lojista não sabe o que é uma porta COM, e não deveria precisar saber. O
instalador varre as portas, identifica a balança pelo protocolo que ela
responde e localiza a impressora térmica — gravando tudo em `device_settings`.

Cuidados que esta varredura toma
--------------------------------

**Sondar porta serial não é inofensivo.** Enviar `ENQ` para um equipamento que
não é balança pode confundi-lo: uma impressora serial pode cuspir papel, um
leitor pode travar. Por isso a ordem é:

1. **Escuta passiva primeiro.** Protocolos de streaming (Filizola) são
   identificados só ouvindo, sem escrever nada na porta.
2. **Requisição depois.** Só então tentamos `ENQ` (Toledo, Urano), e apenas em
   portas que não responderam passivamente.

**Descrição da porta ajuda, mas não decide.** O nome que o Windows dá ao
dispositivo (`USB-SERIAL CH340`, `Prolific`, `FTDI`) é uma pista útil para
ordenar as tentativas, nunca uma conclusão — o mesmo chip aparece em balança,
leitor e gaveta. A conclusão vem de conseguir *ler um peso válido*.

**Confiança é medida, não assumida.** Uma leitura válida pode ser coincidência
de bytes. Exigimos várias leituras consistentes antes de declarar uma balança.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from pdv.config import ScaleConfig
from pdv.domain.errors import ScaleError
from pdv.hardware.scale.base import ScaleDriver
from pdv.hardware.scale.protocols import PROTOCOL_REGISTRY
from pdv.hardware.scale.serial_scale import SerialScale

logger = logging.getLogger(__name__)

#: Leituras válidas exigidas para declarar que a porta tem uma balança.
REQUIRED_VALID_READS = 3

#: Tentativas por porta/protocolo antes de desistir.
PROBE_ATTEMPTS = 5

#: Pistas no nome do dispositivo que sugerem conversor USB-serial.
#: Ordenam as tentativas; não concluem nada sozinhas.
SERIAL_ADAPTER_HINTS = ("ch340", "ch341", "prolific", "pl2303", "ftdi", "ft232", "cp210")


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    device: str
    description: str = ""
    hwid: str = ""

    @property
    def looks_like_adapter(self) -> bool:
        haystack = f"{self.description} {self.hwid}".lower()
        return any(hint in haystack for hint in SERIAL_ADAPTER_HINTS)


@dataclass(frozen=True, slots=True)
class DetectedScale:
    port: str
    protocol: str
    baudrate: int
    valid_reads: int
    sample_frame: str

    @property
    def confidence(self) -> str:
        if self.valid_reads >= REQUIRED_VALID_READS + 1:
            return "alta"
        if self.valid_reads >= REQUIRED_VALID_READS:
            return "media"
        return "baixa"


@dataclass(frozen=True, slots=True)
class DetectedPrinter:
    name: str
    is_thermal_candidate: bool
    is_default: bool = False


@dataclass(frozen=True, slots=True)
class DetectionResult:
    scales: tuple[DetectedScale, ...] = ()
    printers: tuple[DetectedPrinter, ...] = ()
    ports_scanned: int = 0

    @property
    def best_scale(self) -> DetectedScale | None:
        """A balança com mais leituras válidas."""
        if not self.scales:
            return None
        return max(self.scales, key=lambda s: s.valid_reads)

    @property
    def best_printer(self) -> DetectedPrinter | None:
        """Só devolve térmica reconhecida. Sem candidata, devolve `None`.

        **Não** cai para a impressora padrão de propósito. A padrão costuma ser
        um jato de tinta ou "Microsoft Print to PDF", e mandar ESC/POS cru para
        lá produz páginas de lixo (ou uma caixa de diálogo travando o caixa).
        Melhor deixar o operador escolher — `None` leva o app ao backend de
        arquivo, que é inofensivo e visível.
        """
        thermal = [p for p in self.printers if p.is_thermal_candidate]
        return thermal[0] if thermal else None


class DriverFactory(Protocol):
    """Cria um driver para sondar uma porta. Injetável para testar sem hardware."""

    def __call__(self, port: str, protocol_name: str, baudrate: int) -> ScaleDriver: ...


def _default_driver_factory(port: str, protocol_name: str, baudrate: int) -> ScaleDriver:
    return SerialScale(
        ScaleConfig(
            protocol=protocol_name,  # type: ignore[arg-type]
            port=port,
            baudrate=baudrate,
            timeout_seconds=0.5,
        )
    )


def list_serial_ports() -> list[SerialPortInfo]:
    """Portas seriais disponíveis. Lista vazia se o pyserial não estiver presente."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover
        logger.warning("pyserial ausente: detecção de balança indisponível")
        return []

    return [
        SerialPortInfo(
            device=port.device,
            description=port.description or "",
            hwid=port.hwid or "",
        )
        for port in list_ports.comports()
    ]


def probe_port(
    port: SerialPortInfo,
    *,
    baudrates: tuple[int, ...] = (9600, 4800, 19200),
    driver_factory: DriverFactory = _default_driver_factory,
) -> DetectedScale | None:
    """Tenta identificar uma balança numa porta.

    Ordem deliberada: protocolos de **streaming** primeiro (só escutam), depois
    os de **requisição** (escrevem na porta). Assim um equipamento que não é
    balança tem menos chance de receber bytes que não esperava.
    """
    streaming = [
        name for name, cls in PROTOCOL_REGISTRY.items() if cls().is_streaming
    ]
    on_request = [
        name for name, cls in PROTOCOL_REGISTRY.items() if not cls().is_streaming
    ]

    for baudrate in baudrates:
        for protocol_name in [*streaming, *on_request]:
            detected = _try_protocol(port, protocol_name, baudrate, driver_factory)
            if detected is not None:
                return detected

    return None


def _try_protocol(
    port: SerialPortInfo,
    protocol_name: str,
    baudrate: int,
    driver_factory: DriverFactory,
) -> DetectedScale | None:
    driver = driver_factory(port.device, protocol_name, baudrate)

    try:
        driver.open()
    except ScaleError:
        # Porta ocupada ou inexistente: não é erro de detecção, é o cenário
        # normal de uma máquina com várias portas.
        return None

    valid = 0
    sample = ""
    try:
        for _ in range(PROBE_ATTEMPTS):
            try:
                reading = driver.read()
            except ScaleError:
                continue

            # Peso zerado conta como leitura válida: balança vazia é o estado
            # mais comum durante a instalação. O que importa é o quadro ter
            # chegado no formato esperado pelo protocolo.
            valid += 1
            sample = sample or reading.raw_frame

            if valid >= REQUIRED_VALID_READS:
                break
    finally:
        driver.close()

    if valid < REQUIRED_VALID_READS:
        return None

    logger.info(
        "Balança detectada em %s: %s @ %d (%d leituras)",
        port.device, protocol_name, baudrate, valid,
    )
    return DetectedScale(
        port=port.device,
        protocol=protocol_name,
        baudrate=baudrate,
        valid_reads=valid,
        sample_frame=sample,
    )


def detect_scales(
    ports: list[SerialPortInfo] | None = None,
    *,
    driver_factory: DriverFactory = _default_driver_factory,
) -> list[DetectedScale]:
    """Varre as portas em busca de balanças.

    Portas com cara de conversor USB-serial vão primeiro: a balança quase sempre
    está numa delas, e achar logo encurta uma varredura que, no pior caso, leva
    dezenas de segundos.
    """
    available = ports if ports is not None else list_serial_ports()
    ordered = sorted(available, key=lambda p: not p.looks_like_adapter)

    found: list[DetectedScale] = []
    for port in ordered:
        detected = probe_port(port, driver_factory=driver_factory)
        if detected is not None:
            found.append(detected)

    return found


# --------------------------------------------------------------------------- #
# Impressora
# --------------------------------------------------------------------------- #

#: Modelos de impressora térmica de cupom.
#:
#: Atenção ao nível de especificidade: a marca sozinha **não** serve. "epson"
#: casaria com a L8180, que é multifuncional a jato de tinta — e mandar ESC/POS
#: cru para um jato de tinta cospe páginas de lixo. A linha POS da Epson usa o
#: prefixo `TM-`; é esse o padrão que identifica, não o nome da fabricante.
THERMAL_HINTS = (
    "tm-t", "tm-u", "tm-m", "tm-p",          # Epson — linha POS
    "mp-4200", "mp-2800", "mp-100",          # Bematech
    "i9", "i7", "i8",                         # Elgin
    "dr700", "dr800",                         # Daruma
    "pos-", "pos58", "pos80",                 # genéricas
    "termica", "térmica", "thermal", "receipt", "cupom",
)

#: Pistas que **excluem** a impressora, mesmo que algo acima case.
#: Impressora virtual e multifuncional de escritório nunca são destino de cupom.
NON_THERMAL_HINTS = (
    "series",          # nomenclatura típica de multifuncional de consumo
    "inkjet", "deskjet", "officejet", "laserjet", "ecotank",
    "print to pdf", "xps", "onenote", "fax", "microsoft",
    "adobe", "pdfcreator", "send to",
)


def _is_thermal(name: str) -> bool:
    lowered = name.lower()
    if any(hint in lowered for hint in NON_THERMAL_HINTS):
        return False
    return any(hint in lowered for hint in THERMAL_HINTS)


def detect_printers(
    enumerator: Callable[[], list[tuple[str, bool]]] | None = None,
) -> list[DetectedPrinter]:
    """Lista impressoras instaladas, marcando as candidatas a térmica.

    `enumerator` devolve `(nome, é_padrão)` e existe para o teste rodar sem
    depender das impressoras instaladas na máquina do desenvolvedor.
    """
    entries = enumerator() if enumerator is not None else _enumerate_windows_printers()

    return [
        DetectedPrinter(
            name=name,
            is_thermal_candidate=_is_thermal(name),
            is_default=is_default,
        )
        for name, is_default in entries
    ]


def _enumerate_windows_printers() -> list[tuple[str, bool]]:
    try:
        import win32print
    except ImportError:  # pragma: no cover
        logger.warning("pywin32 ausente: detecção de impressora indisponível")
        return []

    try:
        default_name = win32print.GetDefaultPrinter()
    except Exception:  # pragma: no cover - nenhuma impressora instalada
        default_name = ""

    printers = win32print.EnumPrinters(
        win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    )
    return [(info[2], info[2] == default_name) for info in printers]


# --------------------------------------------------------------------------- #
# Varredura completa
# --------------------------------------------------------------------------- #


def detect_all(
    *,
    driver_factory: DriverFactory = _default_driver_factory,
    printer_enumerator: Callable[[], list[tuple[str, bool]]] | None = None,
) -> DetectionResult:
    ports = list_serial_ports()
    return DetectionResult(
        scales=tuple(detect_scales(ports, driver_factory=driver_factory)),
        printers=tuple(detect_printers(printer_enumerator)),
        ports_scanned=len(ports),
    )


def settings_from_detection(result: DetectionResult) -> dict[str, str]:
    """Converte a detecção em chaves para o `SettingsStore`.

    Sem balança detectada, cai para o **modo simulado** em vez de gravar uma
    porta inventada: melhor um PDV que abre e avisa "balança não encontrada" do
    que um que trava tentando ler de uma COM que não existe.
    """
    values: dict[str, str] = {}

    scale = result.best_scale
    if scale is not None:
        values["scale.protocol"] = scale.protocol
        values["scale.port"] = scale.port
        values["scale.baudrate"] = str(scale.baudrate)
    else:
        values["scale.protocol"] = "simulated"

    printer = result.best_printer
    if printer is not None:
        values["printer.backend"] = "win32raw"
        values["printer.name"] = printer.name
    else:
        values["printer.backend"] = "file"

    return values
