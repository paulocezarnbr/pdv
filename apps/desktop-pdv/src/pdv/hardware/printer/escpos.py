"""Construtor de payload ESC/POS bruto para Epson TM-T20X (80 mm).

Por que montar os bytes à mão em vez de usar só a `python-escpos`:

* O payload vira um `bytes` **puro e determinístico**, comparável em teste
  unitário. Dá para validar o layout inteiro do cupom sem impressora ligada —
  e cupom errado só se descobre no hardware, geralmente na frente do cliente.
* O mesmo payload alimenta os dois backends (win32print RAW e libusb), sem
  reescrever o layout para cada um.
* Guilhotina e gaveta são comandos de uma linha; não justificam a dependência.

Referência de comandos (Epson ESC/POS Command Reference):

| Função        | Bytes            | Observação                              |
|---------------|------------------|------------------------------------------|
| Inicializar   | ``1B 40``        | ESC @ — limpa buffer e formatação        |
| Code page     | ``1B 74 02``     | ESC t 2 = PC850 (acentuação pt-BR)       |
| Alinhamento   | ``1B 61 n``      | 0 esq · 1 centro · 2 dir                 |
| Negrito       | ``1B 45 n``      | ESC E                                    |
| Sublinhado    | ``1B 2D n``      | ESC -                                    |
| Tamanho       | ``1D 21 n``      | GS ! — nibble alto largura, baixo altura |
| Avanço        | ``1B 64 n``      | ESC d n linhas                           |
| Corte parcial | ``1D 56 42 n``   | GS V 66 n — avança n e guilhotina        |
| Gaveta        | ``1B 70 m t1 t2``| ESC p — pulso no conector DK             |
"""

from __future__ import annotations

from typing import Final, Self

ESC: Final[int] = 0x1B
GS: Final[int] = 0x1D
LF: Final[int] = 0x0A

#: Fonte A em papel de 80 mm.
DEFAULT_COLUMNS: Final[int] = 48

#: PC850 Multilingual — cobre acentuação portuguesa na TM-T20X.
CODEPAGE_PC850: Final[int] = 2
ENCODING_PC850: Final[str] = "cp850"


class Align:
    LEFT: Final[int] = 0
    CENTER: Final[int] = 1
    RIGHT: Final[int] = 2


class EscPosBuilder:
    """Acumula comandos ESC/POS. Encadeável e sem efeito colateral externo."""

    def __init__(
        self,
        columns: int = DEFAULT_COLUMNS,
        codepage: int = CODEPAGE_PC850,
        encoding: str = ENCODING_PC850,
    ) -> None:
        self._buffer = bytearray()
        self.columns = columns
        self._codepage = codepage
        self._encoding = encoding

    # -- saída ---------------------------------------------------------------- #

    def build(self) -> bytes:
        """Payload final, pronto para ir cru à impressora."""
        return bytes(self._buffer)

    def _raw(self, *values: int) -> Self:
        self._buffer.extend(values)
        return self

    # -- controle ------------------------------------------------------------- #

    def initialize(self) -> Self:
        """ESC @ + code page. Sempre a primeira coisa do cupom.

        Sem o reset, o cupom herda negrito/tamanho de uma impressão anterior
        que tenha falhado no meio.
        """
        self._raw(ESC, 0x40)
        self._raw(ESC, 0x74, self._codepage)
        return self

    def align(self, mode: int) -> Self:
        return self._raw(ESC, 0x61, mode)

    def bold(self, enabled: bool = True) -> Self:
        return self._raw(ESC, 0x45, 1 if enabled else 0)

    def underline(self, enabled: bool = True) -> Self:
        return self._raw(ESC, 0x2D, 1 if enabled else 0)

    def size(self, width: int = 1, height: int = 1) -> Self:
        """GS ! — multiplicador de 1 a 8 em cada eixo."""
        w = max(1, min(8, width)) - 1
        h = max(1, min(8, height)) - 1
        return self._raw(GS, 0x21, (w << 4) | h)

    def reset_style(self) -> Self:
        return self.bold(False).underline(False).size(1, 1).align(Align.LEFT)

    # -- texto ---------------------------------------------------------------- #

    def text(self, value: str) -> Self:
        """Texto sem quebra de linha, codificado na code page da impressora.

        Acento que não existir na PC850 vira ``?`` em vez de estourar exceção:
        um nome de produto exótico não pode impedir a venda de ser impressa.
        """
        self._buffer.extend(value.encode(self._encoding, errors="replace"))
        return self

    def line(self, value: str = "") -> Self:
        return self.text(value)._raw(LF)

    def feed(self, lines: int = 1) -> Self:
        return self._raw(ESC, 0x64, max(0, min(255, lines)))

    def separator(self, char: str = "-") -> Self:
        return self.line(char * self.columns)

    def columns_2(self, left: str, right: str, filler: str = " ") -> Self:
        """Duas colunas numa linha de largura fixa.

        O valor da direita é sagrado (é dinheiro): se faltar espaço, quem é
        truncado é o texto da esquerda. Nunca o total.
        """
        available = self.columns - len(right)
        if available < 1:
            return self.line(right[: self.columns])
        left_text = left[:available] if len(left) > available else left
        padding = filler * (available - len(left_text))
        return self.line(f"{left_text}{padding}{right}")

    def columns_3(self, left: str, middle: str, right: str) -> Self:
        """Três colunas: descrição · quantidade · total."""
        right_width = len(right)
        middle_width = len(middle)
        left_width = self.columns - right_width - middle_width - 2
        if left_width < 1:
            return self.columns_2(left, right)
        left_text = left[:left_width].ljust(left_width)
        return self.line(f"{left_text} {middle} {right}")

    def centered(self, value: str) -> Self:
        return self.align(Align.CENTER).line(value).align(Align.LEFT)

    # -- periféricos ---------------------------------------------------------- #

    def cut(self, feed_lines: int = 4, partial: bool = True) -> Self:
        """GS V — guilhotina.

        Corte **parcial** por padrão: deixa uma ponte de papel que segura o
        cupom até o cliente puxar. Com corte total o cupom cai no chão.
        O avanço antes do corte é obrigatório — sem ele a guilhotina corta o
        texto, porque a lâmina fica ~2 cm acima da cabeça térmica.
        """
        mode = 66 if partial else 65
        return self._raw(GS, 0x56, mode, max(0, min(255, feed_lines)))

    def open_drawer(self, pin: int = 2, on_ms: int = 25, off_ms: int = 250) -> Self:
        """ESC p — pulso na gaveta de dinheiro pelo conector DK da impressora.

        Pino 2 é o padrão da maioria das gavetas; algumas usam o pino 5
        (``pin=5`` → m=1). Tempos em unidades de 2 ms, conforme a especificação.
        """
        m = 0 if pin == 2 else 1
        t1 = max(1, min(255, on_ms // 2))
        t2 = max(1, min(255, off_ms // 2))
        return self._raw(ESC, 0x70, m, t1, t2)

    def beep(self, times: int = 1, duration: int = 3) -> Self:
        """ESC ( A — sinal sonoro (disponível em parte dos firmwares)."""
        return self._raw(ESC, 0x28, 0x41, 0x04, 0x00, 0x30, 0x37, times, duration)

    def qrcode(self, data: str, module_size: int = 6) -> Self:
        """QR Code nativo (GS ( k) — usado para NFC-e, cardápio e cashback."""
        payload = data.encode("utf-8", errors="replace")
        length = len(payload) + 3
        pl, ph = length & 0xFF, (length >> 8) & 0xFF
        # Modelo 2
        self._raw(GS, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x32, 0x00)
        # Tamanho do módulo
        self._raw(GS, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, module_size)
        # Correção de erro nível M
        self._raw(GS, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x45, 0x31)
        # Armazena os dados
        self._raw(GS, 0x28, 0x6B, pl, ph, 0x31, 0x50, 0x30)
        self._buffer.extend(payload)
        # Imprime
        return self._raw(GS, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30)


def format_cents(cents: int) -> str:
    """Centavos → ``1.234,56`` no padrão brasileiro, sem passar por float."""
    negative = cents < 0
    value = abs(int(cents))
    reais, remainder = divmod(value, 100)
    formatted = f"{reais:,}".replace(",", ".")
    result = f"{formatted},{remainder:02d}"
    return f"-{result}" if negative else result


def format_grams(grams: int) -> str:
    """Gramas → ``0,847 kg`` com as três casas que a balança entrega."""
    kilos, rest = divmod(abs(int(grams)), 1000)
    sign = "-" if grams < 0 else ""
    return f"{sign}{kilos},{rest:03d} kg"
