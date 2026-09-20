"""Porta fiscal e adaptador open source PyNFe para NFC-e no RJ.

O domínio depende do protocolo `FiscalGateway`, nunca diretamente do PyNFe.
Trocar a biblioteca por uma API externa altera este adaptador, não a reserva
atômica, a contingência ou o estado fiscal da venda.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from lxml import etree  # type: ignore[import-untyped]

RJ_UF = "rj"
NFC_E_MODEL = "nfce"
SERVICE_AVAILABLE = "107"
AUTHORIZED = frozenset({"100", "150"})


class FiscalCommunicationError(RuntimeError):
    """Falha de transporte ou resposta inválida, sem vazar segredo do A1."""


@dataclass(frozen=True, slots=True)
class SefazStatus:
    available: bool
    code: str
    reason: str
    environment: str | None


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    authorized: bool
    code: str
    reason: str
    access_key: str | None
    protocol: str | None
    processed_xml: bytes | None


class FiscalGateway(Protocol):
    def status(self) -> SefazStatus: ...
    def authorize_signed_xml(self, signed_xml: bytes) -> AuthorizationResult: ...


class PyNFeGateway:
    """Comunica NFC-e modelo 65 com a SVRS selecionada pelo PyNFe para o RJ."""

    def __init__(
        self,
        *,
        certificate_path: Path,
        certificate_password: str,
        homologation: bool = True,
        timeout_seconds: float = 15.0,
        communicator_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not certificate_password:
            raise ValueError("A senha do certificado A1 é obrigatória.")
        if timeout_seconds <= 0:
            raise ValueError("O timeout da SEFAZ deve ser positivo.")
        self._certificate_path = certificate_path
        self._certificate_password = certificate_password
        self._homologation = homologation
        self._timeout = timeout_seconds
        self._factory = communicator_factory or _pynfe_communicator

    def status(self) -> SefazStatus:
        try:
            response = self._communicator().status_servico(
                NFC_E_MODEL, timeout=self._timeout,
            )
            root = _response_root(response)
            code = _first_text(root, "cStat") or ""
            reason = _first_text(root, "xMotivo") or "Resposta sem motivo."
            environment = _first_text(root, "tpAmb")
            return SefazStatus(code == SERVICE_AVAILABLE, code, reason, environment)
        except FiscalCommunicationError:
            raise
        except Exception as exc:
            raise FiscalCommunicationError(
                "Não foi possível consultar a SVRS. Confira certificado, rede e relógio."
            ) from exc

    def authorize_signed_xml(self, signed_xml: bytes) -> AuthorizationResult:
        try:
            note = etree.fromstring(signed_xml)
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise FiscalCommunicationError("XML NFC-e assinado é inválido.") from exc
        try:
            result = self._communicator().autorizacao(
                NFC_E_MODEL, note, ind_sinc=1, timeout=self._timeout,
            )
            success, payload = int(result[0]), result[1]
            root = payload if isinstance(payload, etree._Element) else _response_root(payload)
            code = _first_text(root, "cStat") or ""
            reason = _first_text(root, "xMotivo") or "Resposta sem motivo."
            processed = etree.tostring(root, encoding="utf-8", xml_declaration=True)
            return AuthorizationResult(
                authorized=success == 0 and code in AUTHORIZED,
                code=code,
                reason=reason,
                access_key=_first_text(root, "chNFe"),
                protocol=_first_text(root, "nProt"),
                processed_xml=processed,
            )
        except FiscalCommunicationError:
            raise
        except Exception as exc:
            raise FiscalCommunicationError(
                "A SVRS não confirmou a autorização; consulte pela chave antes de reenviar."
            ) from exc

    def _communicator(self) -> Any:
        return self._factory(
            RJ_UF, str(self._certificate_path), self._certificate_password,
            self._homologation,
        )


def _pynfe_communicator(*args: object) -> Any:
    from pynfe.processamento.comunicacao import ComunicacaoSefaz  # type: ignore[import-untyped]
    return ComunicacaoSefaz(*args)


def _response_root(response: Any) -> etree._Element:
    content = getattr(response, "content", None)
    if not content:
        text = getattr(response, "text", "")
        content = text.encode("utf-8") if isinstance(text, str) else text
    if not content:
        raise FiscalCommunicationError("A SEFAZ respondeu sem XML.")
    try:
        return etree.fromstring(content)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise FiscalCommunicationError("A SEFAZ respondeu conteúdo que não é XML.") from exc


def _first_text(root: etree._Element, local_name: str) -> str | None:
    values = root.xpath(f"//*[local-name()='{local_name}']/text()")
    return str(values[-1]).strip() if values else None
