from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from pdv.fiscal.gateway import FiscalCommunicationError, PyNFeGateway


class Response:
    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeCommunicator:
    def __init__(self, status_xml: bytes, authorization: tuple[object, ...] | None = None) -> None:
        self.status_xml = status_xml
        self.authorization = authorization
        self.status_calls: list[tuple[str, float]] = []

    def status_servico(self, model: str, *, timeout: float) -> Response:
        self.status_calls.append((model, timeout))
        return Response(self.status_xml)

    def autorizacao(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
        assert self.authorization is not None
        return self.authorization


def _gateway(fake: FakeCommunicator) -> PyNFeGateway:
    return PyNFeGateway(
        certificate_path=Path("emitente.pfx"), certificate_password="segredo-a1",
        communicator_factory=lambda *_args: fake,
    )


def test_status_107_means_svrs_available() -> None:
    fake = FakeCommunicator(b"""
      <retConsStatServ xmlns="http://www.portalfiscal.inf.br/nfe">
        <tpAmb>2</tpAmb><cStat>107</cStat><xMotivo>Servico em Operacao</xMotivo>
      </retConsStatServ>""")
    status = _gateway(fake).status()
    assert status.available is True
    assert status.code == "107"
    assert status.environment == "2"
    assert fake.status_calls == [("nfce", 15.0)]


def test_non_107_status_does_not_pretend_service_is_online() -> None:
    fake = FakeCommunicator(b"<r><tpAmb>2</tpAmb><cStat>108</cStat><xMotivo>Paralisado</xMotivo></r>")
    status = _gateway(fake).status()
    assert status.available is False
    assert status.reason == "Paralisado"


def test_authorization_extracts_protocol_and_access_key() -> None:
    processed = etree.fromstring(b"""
      <nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">
        <protNFe><infProt><cStat>100</cStat><xMotivo>Autorizado</xMotivo>
        <chNFe>33260900000000000100650010000000011000000010</chNFe>
        <nProt>333260000000001</nProt></infProt></protNFe>
      </nfeProc>""")
    fake = FakeCommunicator(b"", (0, processed))
    result = _gateway(fake).authorize_signed_xml(
        b'<NFe xmlns="http://www.portalfiscal.inf.br/nfe"><infNFe Id="NFe1"/></NFe>'
    )
    assert result.authorized is True
    assert result.code == "100"
    assert result.protocol == "333260000000001"
    assert result.access_key == "33260900000000000100650010000000011000000010"
    assert result.processed_xml is not None


def test_library_success_without_fiscal_status_is_not_authorized() -> None:
    rejected = etree.fromstring(b"<ret><cStat>539</cStat><xMotivo>Duplicidade</xMotivo></ret>")
    result = _gateway(FakeCommunicator(b"", (0, rejected))).authorize_signed_xml(b"<NFe/>")
    assert result.authorized is False
    assert result.code == "539"


def test_certificate_password_is_never_leaked_by_adapter_errors() -> None:
    secret = "segredo-super-sensivel"

    def broken(*_args: object):  # noqa: ANN202
        raise RuntimeError(secret)

    gateway = PyNFeGateway(certificate_path=Path("emitente.pfx"),
                           certificate_password=secret, communicator_factory=broken)
    with pytest.raises(FiscalCommunicationError) as caught:
        gateway.status()
    assert secret not in str(caught.value)


def test_pinned_pynfe_maps_rj_nfce_to_svrs() -> None:
    from pynfe.processamento.comunicacao import ComunicacaoSefaz

    communicator = ComunicacaoSefaz("rj", "emitente.pfx", "senha", True)
    url = communicator._get_url("nfce", "STATUS")  # contrato externo pinado na versão 0.6.5
    assert url == (
        "https://nfce-homologacao.svrs.rs.gov.br/ws/"
        "NfeStatusServico/NfeStatusServico4.asmx"
    )
