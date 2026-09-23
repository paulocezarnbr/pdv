from __future__ import annotations

from dataclasses import dataclass

from pdv.domain.models import EntityId
from pdv.fiscal.cloud import (
    CloudDefinitelyOffline,
    CloudFiscalDocument,
    CloudResultUnknown,
    FiscalCoordinator,
    HttpCloudFiscalGateway,
)


class FakeCloud:
    def __init__(self, result: CloudFiscalDocument | Exception) -> None:
        self.result = result
        self.calls = 0

    def issue(self, *, request_uuid: str, order_id: str) -> CloudFiscalDocument:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def status(self, request_uuid: str) -> CloudFiscalDocument:
        assert request_uuid
        assert not isinstance(self.result, Exception)
        return self.result


@dataclass
class FakeLocal:
    calls: int = 0
    #: A venda tem valor? Falso simula a cortesia de 100%.
    required: bool = True

    def requires_document(self, _order_id: object) -> bool:
        return self.required

    def reserve(self, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        return "local-document"


def cloud_document() -> CloudFiscalDocument:
    return CloudFiscalDocument("req", "order", "authorized", 1, 7, "3" * 44, "123")


def test_cloud_authorized_never_consumes_local_series() -> None:
    local = FakeLocal()
    decision = FiscalCoordinator(FakeCloud(cloud_document()), local).issue(
        request_uuid="req", order_id=EntityId("order")
    )
    assert decision.mode == "cloud"
    assert local.calls == 0


def test_definite_connection_failure_enters_local_contingency() -> None:
    local = FakeLocal()
    decision = FiscalCoordinator(FakeCloud(CloudDefinitelyOffline()), local).issue(
        request_uuid="req", order_id=EntityId("order")
    )
    assert decision.mode == "local_contingency"
    assert local.calls == 1


def test_ambiguous_timeout_never_issues_local_duplicate() -> None:
    local = FakeLocal()
    decision = FiscalCoordinator(FakeCloud(CloudResultUnknown("timeout")), local).issue(
        request_uuid="req", order_id=EntityId("order")
    )
    assert decision.mode == "unknown"
    assert local.calls == 0


def test_cloud_url_accepts_origin_or_api_root() -> None:
    origin = HttpCloudFiscalGateway("https://erp.example", "token")
    api = HttpCloudFiscalGateway("https://erp.example/api/", "token")
    assert origin._endpoint("fiscal/issue") == "https://erp.example/api/fiscal/issue"
    assert api._endpoint("fiscal/issue") == "https://erp.example/api/fiscal/issue"


# --------------------------------------------------------------------------- #
# Venda com total zero: desconto de 100% ou produto de preço zero
# --------------------------------------------------------------------------- #


def test_a_zero_total_sale_asks_nobody_and_emits_nothing() -> None:
    """Nem nuvem nem série local: não há nota para uma venda de R$ 0,00."""
    cloud = FakeCloud(cloud_document())
    local = FakeLocal(required=False)

    decision = FiscalCoordinator(cloud, local).issue(
        request_uuid="req", order_id=EntityId("order")
    )

    assert decision.mode == "not_required"
    assert cloud.calls == 0
    assert local.calls == 0


def test_an_offline_courtesy_never_reaches_the_contingency_series() -> None:
    """O caso que a ordem das checagens protege.

    Se "precisa de nota?" fosse perguntado à nuvem, uma cortesia feita com a
    internet fora cairia no ramo de contingência e consumiria um número da
    série local — que depois exigiria inutilização.
    """
    local = FakeLocal(required=False)

    decision = FiscalCoordinator(FakeCloud(CloudDefinitelyOffline()), local).issue(
        request_uuid="req", order_id=EntityId("order")
    )

    assert decision.mode == "not_required"
    assert local.calls == 0


def test_the_cloud_saying_not_required_is_obeyed() -> None:
    """A nuvem é a autoridade sobre o pedido sincronizado."""
    cloud = FakeCloud(CloudFiscalDocument("req", "order", "not_required", 0, 0,
                                          reason="Venda com total zero"))

    decision = FiscalCoordinator(cloud, FakeLocal()).issue(
        request_uuid="req", order_id=EntityId("order")
    )

    assert decision.mode == "not_required"


def test_the_gateway_reads_a_not_required_answer_without_series() -> None:
    """A resposta `not_required` não traz série nem número.

    Sem este cuidado, o parse exigiria os dois campos e transformaria a
    cortesia em `CloudResultUnknown` — "resposta ilegível" —, deixando a venda
    esperando uma nota que nunca vai existir.
    """
    import httpx

    def reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "request_uuid": "req", "order_id": "order", "status": "not_required",
            "provider_reason": "Venda com total zero: não se emite NFC-e.",
        })

    gateway = HttpCloudFiscalGateway("https://erp.example", "token")
    gateway._client = httpx.Client(
        base_url="https://erp.example", transport=httpx.MockTransport(reply)
    )

    document = gateway.issue(request_uuid="req", order_id="order")

    assert document.status == "not_required"
    assert (document.series, document.number) == (0, 0)
