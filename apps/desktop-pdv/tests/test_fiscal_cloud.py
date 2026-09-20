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
