"""Contratos da camada de balança.

Por que uma abstração para algo tão simples quanto "ler um número":

O formato do quadro serial varia entre fabricantes **e entre versões de
firmware do mesmo fabricante** — número de dígitos, presença de byte de status,
casas decimais implícitas e até a paridade. Isolando cada variação em uma classe
`ScaleProtocol`, ajustar uma Toledo de firmware antigo não arrisca quebrar a
Filizola da loja vizinha.

A camada de protocolo é **pura**: recebe bytes, devolve `ScaleReading`. Ela não
abre porta serial, não dorme, não loga. Isso a torna testável com um literal de
bytes, sem hardware nenhum.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final

from pdv.domain.errors import ScaleFrameError
from pdv.domain.models import Grams, ScaleReading, ScaleStatus

# Bytes de controle ASCII usados pelos protocolos de balança
STX: Final[int] = 0x02
ETX: Final[int] = 0x03
ENQ: Final[int] = 0x05
ACK: Final[int] = 0x06
NAK: Final[int] = 0x15
CR: Final[int] = 0x0D


class ScaleProtocol(ABC):
    """Traduz bytes crus ↔ leitura de peso para um modelo de balança."""

    name: str = "generic"

    #: Quadro enviado para pedir uma pesagem. ``None`` = balança transmite sozinha.
    request_frame: bytes | None = bytes([ENQ])

    #: Delimitadores do quadro de resposta.
    start_byte: int = STX
    end_byte: int = ETX

    #: Dígitos de peso no quadro e casas decimais implícitas (em kg).
    weight_digits: int = 5
    weight_decimals: int = 3

    @property
    def is_streaming(self) -> bool:
        """Balança de transmissão contínua não precisa de requisição."""
        return self.request_frame is None

    @abstractmethod
    def parse(self, frame: bytes) -> ScaleReading:
        """Converte o miolo do quadro (sem STX/ETX) em `ScaleReading`.

        Raises:
            ScaleFrameError: quadro incompatível com o protocolo.
        """

    # -- utilidades compartilhadas pelas implementações concretas ------------ #

    def _digits_to_grams(self, digits: str) -> Grams:
        """Converte dígitos ASCII em gramas inteiras.

        Um quadro ``"01234"`` com 3 casas decimais significa 1,234 kg → 1234 g.
        O fator ``10 ** (3 - decimals)`` generaliza para balanças que enviam
        2 casas (centigramas) ou 4.
        """
        if not digits.isdigit():
            raise ScaleFrameError(f"{self.name}: dígitos inválidos {digits!r}")
        factor = 10 ** (3 - self.weight_decimals)
        return Grams(int(digits) * factor)

    def _reading(
        self, status: ScaleStatus, grams: Grams, raw: bytes
    ) -> ScaleReading:
        return ScaleReading(
            status=status,
            weight_grams=grams,
            raw_frame=raw.decode("ascii", errors="backslashreplace"),
        )


class ScaleDriver(ABC):
    """Transporte físico. Sabe abrir porta e obter uma leitura; nada de regra."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> ScaleReading:
        """Obtém uma leitura. Bloqueia até o timeout configurado.

        Raises:
            ScaleError: qualquer falha de porta, timeout ou quadro.
        """

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    def __enter__(self) -> ScaleDriver:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
