"""Protocolos concretos de balança de checkout.

⚠️ **Leia antes de usar em produção.** Os quadros abaixo cobrem as variantes mais
comuns no mercado brasileiro, mas número de dígitos, byte de status, casas
decimais e paridade **mudam entre versões de firmware do mesmo modelo**. Confira
o manual do equipamento e, na homologação, registre o quadro cru com
`tools/scale_sniffer.py` antes de fechar a configuração da loja.

É exatamente por essa variabilidade que cada modelo é uma classe isolada:
corrigir a Toledo de uma loja não pode arriscar a Filizola de outra.

Resumo das variantes implementadas:

| Modelo             | Modo          | Quadro                         | Serial   |
|--------------------|---------------|--------------------------------|----------|
| Toledo Prix 3 / 4  | Requisição ENQ| STX + 5–6 dígitos + ETX        | 9600 8N1 |
| Filizola           | Streaming     | STX + 5 dígitos [+ extras] ETX | 9600 8N1 |
| Urano POP / UDC    | Requisição ENQ| STX + [status] + 5 díg. + ETX  | 9600 8N1 |
"""

from __future__ import annotations

from pdv.domain.errors import ScaleFrameError
from pdv.domain.models import Grams, ScaleReading, ScaleStatus
from pdv.hardware.scale.base import ENQ, ScaleProtocol


class ToledoPrix3Protocol(ScaleProtocol):
    """Toledo Prix 3 / Prix 4 — modo requisição.

    O PDV envia ``ENQ`` (0x05) e a balança responde ``STX`` + peso + ``ETX``.
    O peso vem em ASCII, sem ponto decimal, com 3 casas implícitas em kg:
    ``b"\\x0201234\\x03"`` → 1,234 kg.

    Condições anômalas chegam como letras no lugar dos dígitos — a balança
    literalmente escreve o problema no quadro:

    * ``I`` → peso instável (prato balançando)
    * ``S`` → sobrecarga (acima da capacidade)
    * ``N`` → peso negativo (prato removido / precisa tarar)
    """

    name = "toledo_prix3"
    request_frame = bytes([ENQ])
    weight_digits = 5
    weight_decimals = 3

    _ANOMALIES = {
        "I": ScaleStatus.UNSTABLE,
        "S": ScaleStatus.OVERLOAD,
        "N": ScaleStatus.NEGATIVE,
    }

    def parse(self, frame: bytes) -> ScaleReading:
        body = frame.decode("ascii", errors="replace").strip()
        if not body:
            raise ScaleFrameError("toledo: quadro vazio")

        for marker, status in self._ANOMALIES.items():
            if marker in body.upper():
                return self._reading(status, Grams(0), frame)

        digits = "".join(ch for ch in body if ch.isdigit())
        # Firmwares de 6 dígitos apenas prefixam um zero; os 5 finais bastam.
        if len(digits) > self.weight_digits:
            digits = digits[-self.weight_digits :]
        if len(digits) != self.weight_digits:
            raise ScaleFrameError(
                f"toledo: esperados {self.weight_digits} dígitos, recebido {body!r}"
            )

        grams = self._digits_to_grams(digits)
        status = ScaleStatus.ZERO if grams == 0 else ScaleStatus.STABLE
        return self._reading(status, grams, frame)


class FilizolaProtocol(ScaleProtocol):
    """Filizola — transmissão contínua (streaming).

    A balança envia quadros ininterruptamente, sem ser perguntada. O leitor
    precisa então **sempre pegar o último quadro completo** do buffer, não o
    primeiro: o primeiro pode ter minutos de idade e refletir o cliente anterior
    — cobrar o peso errado do cliente errado.

    Modelos com display de preço enviam campos extras (tara, preço/kg, total)
    após o peso. Consumimos apenas os 5 primeiros dígitos e ignoramos o resto,
    o que mantém o driver compatível com as duas famílias.
    """

    name = "filizola"
    request_frame = None  # streaming
    weight_digits = 5
    weight_decimals = 3

    def parse(self, frame: bytes) -> ScaleReading:
        body = frame.decode("ascii", errors="replace").strip()
        if not body:
            raise ScaleFrameError("filizola: quadro vazio")

        upper = body.upper()
        if "I" in upper:
            return self._reading(ScaleStatus.UNSTABLE, Grams(0), frame)
        if "S" in upper:
            return self._reading(ScaleStatus.OVERLOAD, Grams(0), frame)
        if body.startswith("-"):
            return self._reading(ScaleStatus.NEGATIVE, Grams(0), frame)

        digits = "".join(ch for ch in body if ch.isdigit())
        if len(digits) < self.weight_digits:
            raise ScaleFrameError(f"filizola: quadro curto {body!r}")

        grams = self._digits_to_grams(digits[: self.weight_digits])
        status = ScaleStatus.ZERO if grams == 0 else ScaleStatus.STABLE
        return self._reading(status, grams, frame)


class UranoProtocol(ScaleProtocol):
    """Urano POP-Z / UDC — modo requisição.

    Responde a ``ENQ`` com ``STX`` + peso + ``ETX``. Parte da linha insere um
    byte de status antes dos dígitos:

    * ``0`` ou ausente → peso estável
    * ``1`` → instável
    * ``2`` → sobrecarga
    * ``3`` → peso negativo
    """

    name = "urano"
    request_frame = bytes([ENQ])
    weight_digits = 5
    weight_decimals = 3

    _STATUS_BYTES = {
        "0": ScaleStatus.STABLE,
        "1": ScaleStatus.UNSTABLE,
        "2": ScaleStatus.OVERLOAD,
        "3": ScaleStatus.NEGATIVE,
    }

    def parse(self, frame: bytes) -> ScaleReading:
        body = frame.decode("ascii", errors="replace").strip()
        if not body:
            raise ScaleFrameError("urano: quadro vazio")

        status = ScaleStatus.STABLE
        # Byte de status presente apenas quando sobra um dígito além do peso.
        if len(body) == self.weight_digits + 1 and body[0] in self._STATUS_BYTES:
            status = self._STATUS_BYTES[body[0]]
            body = body[1:]

        if status is not ScaleStatus.STABLE:
            return self._reading(status, Grams(0), frame)

        digits = "".join(ch for ch in body if ch.isdigit())
        if len(digits) != self.weight_digits:
            raise ScaleFrameError(
                f"urano: esperados {self.weight_digits} dígitos, recebido {body!r}"
            )

        grams = self._digits_to_grams(digits)
        final = ScaleStatus.ZERO if grams == 0 else ScaleStatus.STABLE
        return self._reading(final, grams, frame)


PROTOCOL_REGISTRY: dict[str, type[ScaleProtocol]] = {
    ToledoPrix3Protocol.name: ToledoPrix3Protocol,
    FilizolaProtocol.name: FilizolaProtocol,
    UranoProtocol.name: UranoProtocol,
}


def build_protocol(name: str) -> ScaleProtocol:
    """Fábrica pelo nome configurado em `ScaleConfig.protocol`."""
    try:
        return PROTOCOL_REGISTRY[name]()
    except KeyError:
        known = ", ".join(sorted(PROTOCOL_REGISTRY))
        raise ScaleFrameError(
            f"Protocolo de balança desconhecido: {name!r}. Disponíveis: {known}"
        ) from None
