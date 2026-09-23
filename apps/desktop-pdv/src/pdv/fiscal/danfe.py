"""DANFE NFC-e em 80 mm — o documento auxiliar que o consumidor leva.

O que este módulo faz, e o que ele se recusa a fazer
----------------------------------------------------

Ele **imprime** o que veio do documento fiscal: chave, protocolo, QR Code,
URL de consulta, itens e pagamentos. Ele **não calcula** nenhum valor fiscal e
**não monta** o QR Code. O conteúdo do QR (`infNFeSupl/qrCode`) e a URL de
consulta (`infNFeSupl/urlChave`) saem do XML assinado pelo motor fiscal; se o
DANFE os montasse por conta própria, bastaria uma divergência de versão do QR
Code para imprimir, em papel, um documento que a SEFAZ não reconhece — e o
consumidor só descobriria ao tentar consultar.

E ele **recusa imprimir** um documento incoerente. As checagens abaixo não são
burocracia: um DANFE cuja série não bate com a série dentro da própria chave de
acesso, ou que diz "normal" sem protocolo de autorização, é um papel com cara de
nota fiscal que não corresponde a nota nenhuma. Na fiscalização, isso é pior
que não ter entregado nada.

A chave de acesso carrega o documento dentro dela
-------------------------------------------------

Os 44 dígitos não são um identificador opaco. Eles codificam::

    cUF  AAMM  CNPJ            mod série nNF        tpEmis cNF      cDV
    [2]  [4]   [14]            [2] [3]   [9]        [1]    [8]      [1]

Por isso dá para conferir, sem consultar ninguém, que a chave é deste emitente,
deste modelo, desta série, deste número e deste tipo de emissão — e que o
dígito verificador fecha. Uma chave truncada, trocada entre duas vendas ou
digitada à mão falha aqui, e não na mão do fiscal.

Divisões do documento (Manual de Especificações do DANFE NFC-e)
---------------------------------------------------------------

I    Cabeçalho — razão social, CNPJ, IE, endereço
II   Identificação do documento
III  Detalhe da venda
IV   Totais e pagamento
V    Mensagem fiscal — ambiente, contingência, número, série, chave, consulta
VI   Consumidor
VII  QR Code
VIII Protocolo de autorização (ausente em contingência)
IX   Tributos aproximados (Lei 12.741/2012), quando informados
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pdv.config import PrinterConfig
from pdv.domain.models import PaymentMethod
from pdv.hardware.printer.escpos import Align, EscPosBuilder, format_cents
from pdv.hardware.printer.layout import METHOD_LABELS

#: Modelo da NFC-e dentro da chave de acesso.
_MODEL_NFCE = "65"

#: `tpEmis` dentro da chave: 1 = emissão normal, 9 = contingência off-line.
_EMISSION_CODE = {"normal": "1", "offline_contingency": "9"}


class DanfeError(ValueError):
    """O documento não pode ser impresso como DANFE — e o motivo é fiscal."""


@dataclass(frozen=True, slots=True)
class DanfeIssuer:
    legal_name: str
    cnpj: str
    state_registration: str
    address: str


@dataclass(frozen=True, slots=True)
class DanfeItem:
    code: str
    description: str
    quantity: Decimal
    unit: str
    unit_price_cents: int
    total_cents: int


@dataclass(frozen=True, slots=True)
class DanfePayment:
    method: PaymentMethod
    amount_cents: int


@dataclass(frozen=True, slots=True)
class NfceDanfe:
    """Tudo o que vai no papel, vindo do documento fiscal — nada calculado aqui."""

    issuer: DanfeIssuer
    environment: Literal["homologation", "production"]
    emission: Literal["normal", "offline_contingency"]
    series: int
    number: int
    issued_at: datetime
    items: tuple[DanfeItem, ...]
    payments: tuple[DanfePayment, ...]
    access_key: str
    #: `infNFeSupl/urlChave` do XML. Varia por UF e por ambiente.
    consultation_url: str
    #: `infNFeSupl/qrCode` do XML, íntegro. Nunca montado pelo PDV.
    qr_code: str
    discount_cents: int = 0
    change_cents: int = 0
    protocol: str | None = None
    authorized_at: datetime | None = None
    consumer_document: str | None = None
    approximate_taxes_cents: int | None = None

    @property
    def items_total_cents(self) -> int:
        return sum(item.total_cents for item in self.items)

    @property
    def payable_cents(self) -> int:
        return self.items_total_cents - self.discount_cents


# --------------------------------------------------------------------------- #
# Validação
# --------------------------------------------------------------------------- #


def access_key_check_digit(first_43: str) -> str:
    """Dígito verificador da chave de acesso — módulo 11, pesos 2 a 9.

    Os pesos correm da direita para a esquerda, reiniciando em 2 depois do 9.
    Resto 0 ou 1 dá dígito 0: é a regra do Manual de Orientação do Contribuinte
    para a chave da NF-e, que a NFC-e herda.
    """
    if len(first_43) != 43 or not first_43.isdigit():
        raise DanfeError("A base da chave de acesso precisa de 43 dígitos.")
    total = 0
    weight = 2
    for digit in reversed(first_43):
        total += int(digit) * weight
        weight = 2 if weight == 9 else weight + 1
    remainder = total % 11
    return "0" if remainder < 2 else str(11 - remainder)


def validate_danfe(danfe: NfceDanfe) -> None:
    """Recusa o que não pode ir para a mão do consumidor.

    Raises:
        DanfeError: com o motivo, em linguagem de quem vai corrigir.
    """
    key = danfe.access_key
    if len(key) != 44 or not key.isdigit():
        raise DanfeError("A chave de acesso precisa ter 44 dígitos.")
    if access_key_check_digit(key[:43]) != key[43]:
        # Chave truncada, digitada à mão ou corrompida no caminho. Imprimir
        # daria ao consumidor uma consulta que a SEFAZ nunca vai encontrar.
        raise DanfeError("O dígito verificador da chave de acesso não confere.")

    cnpj = "".join(ch for ch in danfe.issuer.cnpj if ch.isdigit())
    if key[6:20] != cnpj:
        raise DanfeError("A chave de acesso é de outro CNPJ, não deste emitente.")
    if key[20:22] != _MODEL_NFCE:
        raise DanfeError("A chave de acesso não é de uma NFC-e (modelo 65).")
    if int(key[22:25]) != danfe.series or int(key[25:34]) != danfe.number:
        # O caso típico é trocar documentos entre duas vendas: o papel diria
        # uma série e um número, e a chave, outros.
        raise DanfeError("A série ou o número não conferem com a chave de acesso.")
    if key[34] != _EMISSION_CODE[danfe.emission]:
        raise DanfeError(
            "O tipo de emissão impresso não confere com o da chave de acesso."
        )

    if danfe.emission == "normal":
        # Na emissão normal o DANFE só existe DEPOIS da autorização. Sem
        # protocolo, este papel afirmaria uma autorização que não aconteceu.
        if not danfe.protocol or danfe.authorized_at is None:
            raise DanfeError(
                "Emissão normal sem protocolo de autorização não vira DANFE."
            )
    elif danfe.protocol:
        raise DanfeError(
            "Documento em contingência não tem protocolo; os dados se contradizem."
        )

    if not danfe.items:
        raise DanfeError("O documento não tem itens.")
    if not danfe.qr_code.strip() or not danfe.consultation_url.strip():
        raise DanfeError("QR Code ou URL de consulta ausentes no documento fiscal.")

    paid = sum(payment.amount_cents for payment in danfe.payments)
    if paid - danfe.change_cents != danfe.payable_cents:
        # O papel mostraria um total e um pagamento que não fecham — o tipo de
        # divergência que o consumidor aponta no balcão e o fiscal na autuação.
        raise DanfeError(
            f"Os pagamentos ({format_cents(paid - danfe.change_cents)}) não "
            f"fecham com o valor a pagar ({format_cents(danfe.payable_cents)})."
        )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def build_nfce_danfe(danfe: NfceDanfe, config: PrinterConfig) -> bytes:
    """Monta o DANFE NFC-e. Valida antes: documento incoerente não imprime."""
    validate_danfe(danfe)

    b = EscPosBuilder(columns=config.columns, codepage=config.codepage)
    b.initialize()

    _issuer(b, danfe)
    _identification(b)
    _items(b, danfe)
    _totals(b, danfe)
    _fiscal_message(b, danfe)
    _consumer(b, danfe)
    _qr_code(b, danfe)
    _authorization(b, danfe)
    _taxes(b, danfe)

    b.feed(1)
    b.cut(feed_lines=config.cut_feed_lines, partial=True)
    return b.build()


def _issuer(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão I."""
    issuer = danfe.issuer
    b.align(Align.CENTER).bold(True)
    for line in _wrap(issuer.legal_name, b.columns):
        b.line(line)
    b.bold(False)
    b.line(f"CNPJ: {format_cnpj(issuer.cnpj)}  IE: {issuer.state_registration}")
    for line in _wrap(issuer.address, b.columns):
        b.line(line)
    b.align(Align.LEFT)
    b.separator()


def _identification(b: EscPosBuilder) -> None:
    """Divisão II."""
    b.align(Align.CENTER)
    b.line("DANFE NFC-e - Documento Auxiliar")
    b.line("da Nota Fiscal de Consumidor Eletrônica")
    b.align(Align.LEFT)
    b.separator()


def _items(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão III.

    Duas linhas por item: descrição em cima, quantidade × unitário = total
    embaixo. Em 48 colunas não cabe o código, a descrição e os três números na
    mesma linha sem truncar justamente a descrição — que é o que o consumidor
    lê para conferir a conta.
    """
    b.bold(True).line("Código  Descrição").bold(False)
    b.columns_2("Qtde Un x Vl Unit", "Vl Total")
    b.separator()
    for item in danfe.items:
        b.line(f"{item.code[:7]:<7} {item.description}"[: b.columns])
        detail = (
            f"   {_quantity(item.quantity)} {item.unit} x "
            f"{format_cents(item.unit_price_cents)}"
        )
        b.columns_2(detail, format_cents(item.total_cents))
    b.separator()


def _totals(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão IV."""
    b.columns_2("Qtde. total de itens", str(len(danfe.items)))
    b.columns_2("Valor total R$", format_cents(danfe.items_total_cents))
    if danfe.discount_cents:
        b.columns_2("Desconto R$", f"-{format_cents(danfe.discount_cents)}")
    b.bold(True)
    b.columns_2("Valor a Pagar R$", format_cents(danfe.payable_cents))
    b.bold(False)
    b.columns_2("FORMA PAGAMENTO", "VALOR PAGO R$")
    for payment in danfe.payments:
        label = METHOD_LABELS.get(payment.method, payment.method.value)
        b.columns_2(label, format_cents(payment.amount_cents))
    if danfe.change_cents:
        b.columns_2("Troco R$", format_cents(danfe.change_cents))
    b.separator()


def _fiscal_message(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão V — é aqui que o papel diz o que ele é."""
    b.align(Align.CENTER)

    if danfe.environment == "homologation":
        # Nota de homologação não vale nada; o papel precisa dizer isso de um
        # jeito que ninguém confunda com uma venda de verdade.
        b.bold(True)
        b.line("EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO")
        b.line("SEM VALOR FISCAL")
        b.bold(False)

    if danfe.emission == "offline_contingency":
        # A indicação visível que a arquitetura fiscal exige: o consumidor
        # recebe um documento que ainda não foi autorizado, e precisa saber.
        b.bold(True).size(1, 2)
        b.line("EMITIDA EM CONTINGÊNCIA")
        b.size(1, 1)
        b.line("Pendente de autorização")
        b.bold(False)

    b.line(
        f"Número {danfe.number:09d}  Série {danfe.series:03d}  "
        f"{danfe.issued_at.astimezone().strftime('%d/%m/%Y %H:%M:%S')}"
    )
    b.feed(1)
    b.line("Consulte pela Chave de Acesso em")
    for line in _wrap(danfe.consultation_url, b.columns):
        b.line(line)
    for line in format_access_key(danfe.access_key):
        b.line(line)
    b.align(Align.LEFT)
    b.separator()


def _consumer(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão VI."""
    b.align(Align.CENTER)
    if danfe.consumer_document:
        b.line(f"CONSUMIDOR - {_format_document(danfe.consumer_document)}")
    else:
        b.line("CONSUMIDOR NÃO IDENTIFICADO")
    b.align(Align.LEFT)


def _qr_code(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão VII — o conteúdo vem íntegro do XML, sem nenhuma edição."""
    b.feed(1).align(Align.CENTER)
    b.qrcode(danfe.qr_code, module_size=4)
    b.align(Align.LEFT)


def _authorization(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão VIII — só existe na emissão normal."""
    if danfe.emission != "normal" or danfe.authorized_at is None:
        return
    b.align(Align.CENTER)
    b.line(f"Protocolo de autorização: {danfe.protocol}")
    b.line(
        "Data de autorização: "
        f"{danfe.authorized_at.astimezone().strftime('%d/%m/%Y %H:%M:%S')}"
    )
    b.align(Align.LEFT)


def _taxes(b: EscPosBuilder, danfe: NfceDanfe) -> None:
    """Divisão IX — Lei 12.741/2012, quando o valor foi informado."""
    if danfe.approximate_taxes_cents is None:
        return
    b.separator()
    b.line("Tributos Totais Incidentes")
    b.columns_2(
        "(Lei Federal 12.741/2012) R$",
        format_cents(danfe.approximate_taxes_cents),
    )


# --------------------------------------------------------------------------- #
# Formatação
# --------------------------------------------------------------------------- #


def format_access_key(key: str) -> list[str]:
    """Chave em grupos de quatro, em duas linhas.

    Onze grupos de quatro com espaço somam 54 caracteres — mais que as 48
    colunas do papel. Numa linha só a impressora quebraria no meio de um
    grupo, e quem digita a chave para consultar erra justamente ali.
    """
    groups = [key[i : i + 4] for i in range(0, len(key), 4)]
    return [" ".join(groups[:6]), " ".join(groups[6:])]


def format_cnpj(cnpj: str) -> str:
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    if len(digits) != 14:
        return cnpj
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def _format_document(document: str) -> str:
    digits = "".join(ch for ch in document if ch.isdigit())
    if len(digits) == 11:
        return f"CPF: {digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if len(digits) == 14:
        return f"CNPJ: {format_cnpj(digits)}"
    return document


def _quantity(value: Decimal) -> str:
    """Quantidade sem zeros à direita: `2`, `0,350` — nunca `2.000`."""
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def _wrap(text: str, width: int) -> list[str]:
    """Quebra por palavra. Uma razão social longa não pode sumir no corte."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # Palavra maior que a linha (uma URL, tipicamente): corta em pedaços.
        while len(word) > width:
            lines.append(word[:width])
            word = word[width:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


__all__ = [
    "DanfeError",
    "DanfeIssuer",
    "DanfeItem",
    "DanfePayment",
    "NfceDanfe",
    "access_key_check_digit",
    "build_nfce_danfe",
    "format_access_key",
    "format_cnpj",
    "validate_danfe",
]
