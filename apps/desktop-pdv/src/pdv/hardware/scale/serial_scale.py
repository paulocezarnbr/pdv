"""Driver serial da balança (pyserial) e simulador para desenvolvimento.

Cuidados que este driver toma e que costumam faltar em integração de balança:

1. **Streaming pega o último quadro, não o primeiro.** Balança contínua enche o
   buffer do SO. Ler o primeiro quadro disponível pode entregar um peso de
   minutos atrás — do cliente anterior. Sempre descartamos até o último quadro
   completo.
2. **Buffer de entrada é limpo antes de cada requisição.** Sem isso, a resposta
   lida é a da requisição anterior, e o erro só aparece quando a fila anda
   rápido (ou seja, no sábado lotado).
3. **Sobrecarga é detectada por limite configurado**, além do byte de status:
   firmware antigo às vezes só satura o valor.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Final

from pdv.config import ScaleConfig
from pdv.domain.errors import (
    ScaleError,
    ScaleFrameError,
    ScaleNotConnectedError,
    ScaleTimeoutError,
)
from pdv.domain.models import Grams, ScaleReading, ScaleStatus
from pdv.hardware.scale.base import ScaleDriver, ScaleProtocol
from pdv.hardware.scale.protocols import ToledoPrix3Protocol, build_protocol

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    import serial

_MAX_BUFFER_BYTES: Final[int] = 4096


def _extract_last_frame(
    buffer: bytearray, start_byte: int, end_byte: int
) -> tuple[bytes | None, bytearray]:
    """Extrai o **último** quadro completo do buffer.

    Retorna o miolo do quadro (sem delimitadores) e o buffer restante, já
    truncado para não crescer indefinidamente quando a balança fala sozinha.
    """
    last_frame: bytes | None = None
    cursor = 0
    while True:
        start = buffer.find(start_byte, cursor)
        if start == -1:
            break
        end = buffer.find(end_byte, start + 1)
        if end == -1:
            break
        last_frame = bytes(buffer[start + 1 : end])
        cursor = end + 1

    remaining = bytearray(buffer[cursor:]) if cursor else buffer
    if len(remaining) > _MAX_BUFFER_BYTES:
        del remaining[:-_MAX_BUFFER_BYTES]
    return last_frame, remaining


class SerialScale(ScaleDriver):
    """Balança física conectada a uma porta COM/USB-serial."""

    def __init__(self, config: ScaleConfig, protocol: ScaleProtocol | None = None) -> None:
        self._config = config
        self._protocol = protocol or build_protocol(config.protocol)
        self._port: serial.Serial | None = None
        self._buffer = bytearray()

    @property
    def protocol(self) -> ScaleProtocol:
        return self._protocol

    @property
    def is_open(self) -> bool:
        return self._port is not None and bool(self._port.is_open)

    def open(self) -> None:
        try:
            import serial  # import tardio: o app abre mesmo sem pyserial instalado
        except ImportError as exc:  # pragma: no cover
            raise ScaleNotConnectedError(
                "pyserial não instalado — execute: pip install pyserial"
            ) from exc

        try:
            self._port = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baudrate,
                bytesize=self._config.bytesize,
                parity=self._config.parity,
                stopbits=self._config.stopbits,
                timeout=self._config.timeout_seconds,
                write_timeout=self._config.timeout_seconds,
            )
        except Exception as exc:  # serial.SerialException e afins
            raise ScaleNotConnectedError(
                f"Não foi possível abrir {self._config.port}: {exc}"
            ) from exc
        self._buffer.clear()

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            finally:
                self._port = None

    def read(self) -> ScaleReading:
        if self._port is None or not self._port.is_open:
            raise ScaleNotConnectedError("Porta serial da balança fechada")

        try:
            frame = (
                self._read_streaming()
                if self._protocol.is_streaming
                else self._read_on_request()
            )
        except ScaleError:
            raise
        except Exception as exc:  # falha de porta, cabo removido, driver do SO
            raise ScaleError(f"Falha de leitura na balança: {exc}") from exc

        if frame is None:
            raise ScaleTimeoutError(
                f"Balança {self._protocol.name} não respondeu em "
                f"{self._config.timeout_seconds:.1f}s"
            )

        reading = self._protocol.parse(frame)
        return self._apply_capacity_guard(reading)

    # -- modos de leitura ---------------------------------------------------- #

    def _read_on_request(self) -> bytes | None:
        """Envia ENQ e aguarda um quadro delimitado por ETX."""
        assert self._port is not None
        assert self._protocol.request_frame is not None

        # Descarta eco/resposta da requisição anterior antes de perguntar de novo.
        self._port.reset_input_buffer()
        self._port.write(self._protocol.request_frame)
        self._port.flush()

        raw = self._port.read_until(expected=bytes([self._protocol.end_byte]))
        if not raw:
            return None

        buffer = bytearray(raw)
        frame, _ = _extract_last_frame(
            buffer, self._protocol.start_byte, self._protocol.end_byte
        )
        if frame is None:
            raise ScaleFrameError(f"Quadro sem delimitadores: {bytes(raw)!r}")
        return frame

    def _read_streaming(self) -> bytes | None:
        """Consome o buffer contínuo e devolve o quadro mais recente."""
        assert self._port is not None

        pending = self._port.in_waiting
        if pending:
            self._buffer.extend(self._port.read(pending))
        else:
            self._buffer.extend(self._port.read(1))  # bloqueia até o timeout

        frame, self._buffer = _extract_last_frame(
            self._buffer, self._protocol.start_byte, self._protocol.end_byte
        )
        return frame

    # -- proteções ----------------------------------------------------------- #

    def _apply_capacity_guard(self, reading: ScaleReading) -> ScaleReading:
        """Trata como sobrecarga o peso acima da capacidade do equipamento."""
        if reading.weight_grams > self._config.max_weight_grams:
            return ScaleReading(
                status=ScaleStatus.OVERLOAD,
                weight_grams=Grams(0),
                raw_frame=reading.raw_frame,
                read_at=reading.read_at,
            )
        return reading


class SimulatedScale(ScaleDriver):
    """Balança simulada — desenvolvimento e teste de UI sem hardware.

    Reproduz o comportamento real: o peso oscila por algumas leituras
    (mercadoria acomodando no prato) e depois estabiliza. Gera quadros no mesmo
    formato do protocolo Toledo, então o caminho de *parsing* é exercitado de
    verdade, não contornado.
    """

    def __init__(self, target_grams: int = 847, settle_after: int = 3) -> None:
        self._protocol = ToledoPrix3Protocol()
        self._target = target_grams
        self._settle_after = settle_after
        self._count = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self._count = 0

    def close(self) -> None:
        self._open = False

    def set_target(self, grams: int) -> None:
        """Usado pela UI de desenvolvimento para simular outra mercadoria."""
        self._target = max(0, grams)
        self._count = 0

    def read(self) -> ScaleReading:
        if not self._open:
            raise ScaleNotConnectedError("Balança simulada fechada")

        time.sleep(0.05)
        self._count += 1
        if self._count <= self._settle_after:
            jitter = random.randint(-25, 25)
            grams = max(0, self._target + jitter)
        else:
            grams = self._target

        frame = f"{grams:05d}".encode("ascii")
        return self._protocol.parse(frame)


def build_scale(config: ScaleConfig) -> ScaleDriver:
    """Fábrica do driver a partir da configuração do terminal."""
    if config.protocol == "simulated":
        return SimulatedScale()
    return SerialScale(config)
