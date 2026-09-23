"""DANFE NFC-e — o que vai para a mão do consumidor, e o que se recusa a ir.

A maior parte deste arquivo testa **recusas**. É proposital: imprimir é fácil;
o valor do módulo está em não imprimir um papel com cara de nota fiscal que não
corresponde a nota nenhuma — série trocada, chave de outro CNPJ, "normal" sem
protocolo. Esses papéis saem sem erro nenhum de qualquer layout ingênuo, e só
aparecem na fiscalização.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pdv.config import PrinterConfig
from pdv.domain.models import PaymentMethod
from pdv.fiscal.danfe import (
    DanfeError,
    DanfeIssuer,
    DanfeItem,
    DanfePayment,
    NfceDanfe,
    access_key_check_digit,
    build_nfce_danfe,
    format_access_key,
)

CNPJ = "12345678000190"
QR = (
    "https://www4.fazenda.rj.gov.br/consultaNFCe/QRCode?p="
    "33260912345678000190650010000000421123456785|2|2|1|ABCDEF0123456789"
)


def make_key(
    *,
    cnpj: str = CNPJ,
    model: str = "65",
    series: int = 1,
    number: int = 42,
    emission: str = "1",
) -> str:
    """Uma chave coerente, com o dígito verificador calculado."""
    base = f"33" f"2609" f"{cnpj}" f"{model}" f"{series:03d}" f"{number:09d}" f"{emission}" "12345678"
    assert len(base) == 43
    return base + access_key_check_digit(base)


def make_danfe(**overrides: object) -> NfceDanfe:
    danfe = NfceDanfe(
        issuer=DanfeIssuer(
            legal_name="Confeitaria Aurora Comércio de Alimentos Ltda",
            cnpj=CNPJ,
            state_registration="12345678",
            address="Rua do Ouvidor, 50 - Centro - Rio de Janeiro/RJ",
        ),
        environment="production",
        emission="normal",
        series=1,
        number=42,
        issued_at=datetime(2026, 9, 23, 15, 30, tzinfo=timezone.utc),
        items=(
            DanfeItem("CAFE", "Café expresso", Decimal("2"), "UN", 700, 1400),
            DanfeItem("TORTA", "Torta de chocolate", Decimal("0.350"), "KG", 8900, 3115),
        ),
        payments=(DanfePayment(PaymentMethod.CASH, 5000),),
        change_cents=485,
        access_key=make_key(),
        consultation_url="www.fazenda.rj.gov.br/nfce/consulta",
        qr_code=QR,
        protocol="333260000012345",
        authorized_at=datetime(2026, 9, 23, 15, 30, 2, tzinfo=timezone.utc),
    )
    return replace(danfe, **overrides)


def printed(danfe: NfceDanfe) -> str:
    """O payload como texto, para procurar o que o papel diz."""
    return build_nfce_danfe(danfe, PrinterConfig()).decode("cp850", errors="replace")


# --------------------------------------------------------------------------- #
# O que é impresso
# --------------------------------------------------------------------------- #


def test_an_authorized_nfce_prints_key_protocol_and_totals() -> None:
    text = printed(make_danfe())

    assert "DANFE NFC-e" in text
    assert "12.345.678/0001-90" in text
    for line in format_access_key(make_key()):
        assert line in text
    assert "Protocolo de autorização: 333260000012345" in text
    assert "Valor a Pagar R$" in text and "45,15" in text
    assert "Troco R$" in text and "4,85" in text
    assert "CONSUMIDOR NÃO IDENTIFICADO" in text


def test_the_qr_code_goes_out_exactly_as_the_fiscal_engine_produced_it() -> None:
    """O PDV não monta o QR: ele vem do XML assinado.

    Se o DANFE editasse o conteúdo (encurtar, reformatar, trocar a versão), o
    papel teria um QR que a SEFAZ não reconhece — e o consumidor só descobriria
    ao tentar consultar.
    """
    payload = build_nfce_danfe(make_danfe(), PrinterConfig())

    assert QR.encode("utf-8") in payload


def test_homologation_says_it_has_no_fiscal_value() -> None:
    text = printed(make_danfe(environment="homologation"))

    assert "EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO" in text
    assert "SEM VALOR FISCAL" in text


def test_production_does_not_carry_the_homologation_warning() -> None:
    assert "HOMOLOGAÇÃO" not in printed(make_danfe())


def test_contingency_is_visible_and_has_no_protocol() -> None:
    """A indicação visível que a arquitetura fiscal exige."""
    danfe = make_danfe(
        emission="offline_contingency",
        access_key=make_key(emission="9"),
        protocol=None,
        authorized_at=None,
    )

    text = printed(danfe)

    assert "EMITIDA EM CONTINGÊNCIA" in text
    assert "Pendente de autorização" in text
    assert "Protocolo" not in text


def test_accents_go_out_in_the_printer_codepage() -> None:
    """A TM-T20X usa PC850; um acento em UTF-8 sairia como lixo no papel."""
    danfe = make_danfe(
        emission="offline_contingency",
        access_key=make_key(emission="9"),
        protocol=None,
        authorized_at=None,
    )

    payload = build_nfce_danfe(danfe, PrinterConfig())

    assert "CONTINGÊNCIA".encode("cp850") in payload


def test_an_identified_consumer_is_printed_formatted() -> None:
    text = printed(make_danfe(consumer_document="12345678909"))

    assert "CPF: 123.456.789-09" in text


def test_approximate_taxes_appear_only_when_informed() -> None:
    assert "12.741" not in printed(make_danfe())
    assert "12.741" in printed(make_danfe(approximate_taxes_cents=812))


def test_the_access_key_fits_the_paper() -> None:
    """Onze grupos de quatro somam 54 caracteres, mais que as 48 colunas.

    Numa linha só a impressora quebraria no meio de um grupo, e quem digita a
    chave para consultar erra justamente ali.
    """
    lines = format_access_key(make_key())

    assert len(lines) == 2
    assert all(len(line) <= 48 for line in lines)
    assert "".join(lines).replace(" ", "") == make_key()


def test_weighed_quantity_prints_without_trailing_zeros() -> None:
    text = printed(make_danfe())

    assert "0,35 KG" in text
    assert "2 UN" in text


# --------------------------------------------------------------------------- #
# O que se recusa a imprimir
# --------------------------------------------------------------------------- #


def test_a_key_with_a_wrong_check_digit_is_refused() -> None:
    """Chave truncada, digitada à mão ou corrompida no caminho."""
    key = make_key()
    broken = key[:43] + str((int(key[43]) + 1) % 10)

    with pytest.raises(DanfeError, match="dígito verificador"):
        build_nfce_danfe(make_danfe(access_key=broken), PrinterConfig())


def test_a_key_from_another_issuer_is_refused() -> None:
    other = make_key(cnpj="98765432000110")

    with pytest.raises(DanfeError, match="outro CNPJ"):
        build_nfce_danfe(make_danfe(access_key=other), PrinterConfig())


def test_a_key_of_another_model_is_refused() -> None:
    nfe = make_key(model="55")

    with pytest.raises(DanfeError, match="modelo 65"):
        build_nfce_danfe(make_danfe(access_key=nfe), PrinterConfig())


def test_series_or_number_that_disagree_with_the_key_are_refused() -> None:
    """O caso típico: documentos trocados entre duas vendas."""
    with pytest.raises(DanfeError, match="série ou o número"):
        build_nfce_danfe(make_danfe(number=43), PrinterConfig())
    with pytest.raises(DanfeError, match="série ou o número"):
        build_nfce_danfe(make_danfe(series=2), PrinterConfig())


def test_contingency_paper_with_a_normal_key_is_refused() -> None:
    """O papel diria "contingência"; a chave, "emissão normal"."""
    danfe = make_danfe(emission="offline_contingency", protocol=None, authorized_at=None)

    with pytest.raises(DanfeError, match="tipo de emissão"):
        build_nfce_danfe(danfe, PrinterConfig())


def test_a_normal_emission_without_protocol_is_refused() -> None:
    """Na emissão normal, o DANFE só existe DEPOIS da autorização.

    Sem protocolo, este papel afirmaria uma autorização que não aconteceu — é o
    documento que o sistema imprimiria se tratasse `processing` ou `unknown`
    como se fossem `authorized`.
    """
    with pytest.raises(DanfeError, match="sem protocolo"):
        build_nfce_danfe(make_danfe(protocol=None), PrinterConfig())


def test_contingency_with_a_protocol_is_contradictory() -> None:
    danfe = make_danfe(emission="offline_contingency", access_key=make_key(emission="9"))

    with pytest.raises(DanfeError, match="contradizem"):
        build_nfce_danfe(danfe, PrinterConfig())


def test_payments_that_do_not_close_the_bill_are_refused() -> None:
    """Total e pagamento que não fecham: o consumidor aponta no balcão."""
    danfe = make_danfe(payments=(DanfePayment(PaymentMethod.PIX, 4000),), change_cents=0)

    with pytest.raises(DanfeError, match="não fecham"):
        build_nfce_danfe(danfe, PrinterConfig())


def test_a_missing_qr_code_is_refused() -> None:
    with pytest.raises(DanfeError, match="QR Code"):
        build_nfce_danfe(make_danfe(qr_code="  "), PrinterConfig())


def test_a_document_without_items_is_refused() -> None:
    with pytest.raises(DanfeError, match="itens"):
        build_nfce_danfe(make_danfe(items=(), payments=(), change_cents=0), PrinterConfig())
