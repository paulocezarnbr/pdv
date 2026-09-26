"""Contrato do DANFE NFC-e — o papel fiscal que o C# tem de imprimir igual, e recusar igual.

O DANFE é o único documento fiscal que o consumidor leva. Dois erros custam
caro, e os dois estão aqui:

* **imprimir diferente.** Chave em outro agrupamento, QR Code com outro
  módulo, ambiente de homologação sem o aviso;
* **imprimir o que não devia.** Chave de outro CNPJ, série trocada,
  "normal" sem protocolo, pagamento que não fecha com o total.

Cada recusa leva a mensagem, porque é ela que o operador lê. Como no cupom, a
hora vai no fuso da máquina, e o contrato leva as horas locais que o Python
imprimiu.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_danfe_contract.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pdv.config import PrinterConfig
from pdv.domain.models import PaymentMethod
from pdv.fiscal.danfe import (
    DanfeError, DanfeIssuer, DanfeItem, DanfePayment, NfceDanfe, access_key_check_digit,
    build_nfce_danfe, format_access_key, format_cnpj,
)

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "danfe.json"
ISSUED = datetime(2026, 9, 25, 15, 4, 5, tzinfo=timezone.utc)
AUTHORIZED = datetime(2026, 9, 25, 15, 4, 9, tzinfo=timezone.utc)
CNPJ = "12345678000190"


def _key(series: int, number: int, emission: str, cnpj: str = CNPJ, model: str = "65") -> str:
    base = f"33{2609}{cnpj}{model}{series:03d}{number:09d}{emission}{12345678:08d}"
    return base + access_key_check_digit(base)


ISSUER = DanfeIssuer(
    legal_name="Confeitaria Dolce Affetto Comércio de Alimentos Finos e Doces Artesanais Ltda",
    cnpj="12.345.678/0001-90", state_registration="12345678",
    address="Rua das Laranjeiras, 1234, Loja B - Laranjeiras - Rio de Janeiro/RJ - CEP 22240-003",
)
ITEMS = (
    DanfeItem("0000001234", "Torta de chocolate belga com cobertura de ganache", Decimal("0.847"), "KG", 4990, 4227),
    DanfeItem("77", "Café", Decimal("2.000"), "UN", 750, 1500),
)
URL = "https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode"
QR = "https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode?p=" + _key(1, 42, "1") + "|3|1"


def _danfe(**changes) -> NfceDanfe:
    base = NfceDanfe(
        issuer=ISSUER, environment="production", emission="normal", series=1, number=42,
        issued_at=ISSUED, items=ITEMS,
        payments=(DanfePayment(PaymentMethod.CASH, 6000), DanfePayment(PaymentMethod.DEBIT, 227)),
        access_key=_key(1, 42, "1"), consultation_url=URL, qr_code=QR, discount_cents=0, change_cents=500,
        protocol="333260000012345", authorized_at=AUTHORIZED,
    )
    return dataclasses.replace(base, **changes)


VALID = {
    "normal_producao": {},
    "homologacao_desconto_cpf_tributos": {
        "environment": "homologation", "discount_cents": 227, "change_cents": 0,
        "payments": (DanfePayment(PaymentMethod.PIX, 5500),),
        "consumer_document": "123.456.789-09", "approximate_taxes_cents": 1234,
    },
    "contingencia_cnpj": {
        "emission": "offline_contingency", "access_key": _key(900, 7, "9"), "series": 900, "number": 7,
        "protocol": None, "authorized_at": None, "change_cents": 0,
        "payments": (DanfePayment(PaymentMethod.CREDIT, 5727),), "consumer_document": "98.765.432/0001-98",
    },
}

INVALID = {
    "chave_curta": {"access_key": _key(1, 42, "1")[:43]},
    "digito_errado": {"access_key": _key(1, 42, "1")[:43] + str((int(_key(1, 42, "1")[43]) + 1) % 10)},
    "outro_cnpj": {"access_key": _key(1, 42, "1", cnpj="99999999000199")},
    "modelo_55": {"access_key": _key(1, 42, "1", model="55")},
    "serie_trocada": {"series": 2},
    "numero_trocado": {"number": 43},
    "emissao_trocada": {"emission": "offline_contingency", "protocol": None, "authorized_at": None},
    "normal_sem_protocolo": {"protocol": None},
    "contingencia_com_protocolo": {"emission": "offline_contingency", "access_key": _key(1, 42, "9")},
    "sem_itens": {"items": ()},
    "sem_qrcode": {"qr_code": "  "},
    "pagamento_nao_fecha": {"change_cents": 499},
}


def _spec(danfe: NfceDanfe) -> dict:
    return {
        "issuer": dataclasses.asdict(danfe.issuer),
        "environment": danfe.environment, "emission": danfe.emission,
        "series": danfe.series, "number": danfe.number,
        "items": [{**dataclasses.asdict(item), "quantity": str(item.quantity)} for item in danfe.items],
        "payments": [{"method": p.method.value, "amount_cents": p.amount_cents} for p in danfe.payments],
        "access_key": danfe.access_key, "consultation_url": danfe.consultation_url, "qr_code": danfe.qr_code,
        "discount_cents": danfe.discount_cents, "change_cents": danfe.change_cents, "protocol": danfe.protocol,
        "issued_local": danfe.issued_at.astimezone().strftime("%Y-%m-%dT%H:%M:%S"),
        "authorized_local": danfe.authorized_at.astimezone().strftime("%Y-%m-%dT%H:%M:%S") if danfe.authorized_at else None,
        "consumer_document": danfe.consumer_document, "approximate_taxes_cents": danfe.approximate_taxes_cents,
    }


def _build() -> dict:
    valid = [{"name": name, **_spec(_danfe(**changes)), "hex": build_nfce_danfe(_danfe(**changes), PrinterConfig()).hex()}
             for name, changes in VALID.items()]
    invalid = []
    for name, changes in INVALID.items():
        danfe = _danfe(**changes)
        try:
            build_nfce_danfe(danfe, PrinterConfig())
        except DanfeError as error:
            invalid.append({"name": name, **_spec(danfe), "error": str(error)})
        else:  # pragma: no cover
            raise AssertionError(f"{name} deveria ser recusado")
    bases = ["3326091234567800019065001000000042112345678", "0" * 43, "9" * 43, "1" * 43, "3526" + "7" * 39]
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_danfe_contract.py. Não edite à mão.",
        "valid": valid,
        "invalid": invalid,
        "check_digits": {base: access_key_check_digit(base) for base in bases},
        "access_key_lines": format_access_key(_key(1, 42, "1")),
        "cnpj": {value: format_cnpj(value) for value in ("12345678000190", "12.345.678/0001-90", "123")},
    }


def test_the_danfe_contract_matches_the_implementation() -> None:
    built = json.loads(json.dumps(_build(), ensure_ascii=False))
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if stored["valid"][0]["issued_local"] != built["valid"][0]["issued_local"]:
        for danfe in stored["valid"] + built["valid"] + stored["invalid"] + built["invalid"]:
            for key in ("hex", "issued_local", "authorized_local"):
                danfe.pop(key, None)
    assert stored == built, "o DANFE mudou: regere o contrato e alinhe o C# (apps/pdv-net)"


def test_every_refusal_is_exercised() -> None:
    assert len(_build()["invalid"]) == len(INVALID)
