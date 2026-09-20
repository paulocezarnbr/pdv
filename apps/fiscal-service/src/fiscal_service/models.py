from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Issuer(BaseModel):
    uf: str = Field(pattern=r"^[A-Z]{2}$")
    cnpj: str = Field(pattern=r"^\d{14}$")
    stateRegistration: str = Field(min_length=2, max_length=20)
    taxRegime: int = Field(ge=1, le=3)
    legalName: str = Field(min_length=2, max_length=60)
    address: dict[str, object]


class Item(BaseModel):
    productId: str
    name: str = Field(min_length=1, max_length=120)
    quantity: str
    unitPriceCents: int = Field(ge=0)
    totalCents: int = Field(ge=0)
    ncm: str = Field(pattern=r"^\d{8}$")
    cfop: str = Field(pattern=r"^\d{4}$")
    cest: str | None = Field(default=None, pattern=r"^\d{7}$")
    unitCode: str = Field(min_length=1, max_length=6)
    origin: int = Field(ge=0, le=8)
    csosn: str | None = Field(default=None, pattern=r"^\d{3}$")
    cstIcms: str | None = Field(default=None, pattern=r"^\d{2}$")
    cstPis: str = Field(pattern=r"^\d{2}$")
    cstCofins: str = Field(pattern=r"^\d{2}$")


class FiscalIntent(BaseModel):
    documentId: str
    requestUuid: str
    orderId: str
    tenantId: str
    storeId: str
    deviceId: str
    model: Literal[65]
    series: int = Field(ge=1, le=999)
    number: int = Field(gt=0)
    environment: Literal["homologation", "production"]
    certificateRef: str = Field(min_length=1, max_length=200)
    cscRef: str = Field(min_length=1, max_length=200)
    cscId: str = Field(min_length=1, max_length=10)
    issuer: Issuer
    totalCents: int = Field(gt=0)
    items: list[Item] = Field(min_length=1, max_length=990)


class StatusRequest(BaseModel):
    request_uuid: str


class FiscalResult(BaseModel):
    status: Literal["authorized", "rejected", "unknown"]
    code: str
    reason: str
    access_key: str | None = None
    protocol: str | None = None
    processed_xml: str | None = None
