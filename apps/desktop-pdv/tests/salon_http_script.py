"""As rotas do salão, como o FastAPI as responde — o gabarito do servidor em C#.

`contracts/salon.json` prova os serviços; este prova a CAMADA HTTP por cima
deles. São coisas diferentes, e o repositório já pagou por confundi-las: o
`from __future__ import annotations` no `server.py` fez toda rota responder 422
com os serviços 100% verdes. O app do garçom (`edge/webapp`) é reaproveitado
sem mudar uma linha pelo servidor em C#, então status, corpo e os cabeçalhos
que ele lê (`X-Auth-Scope`) precisam ser os daqui.

Cada passo é uma requisição: método, caminho, as credenciais por nome (o
aparelho, a sessão do garçom, a concessão do gerente) e o corpo. O resultado
guarda o status, o tipo do conteúdo, os cabeçalhos que importam e o corpo
normalizado como no `salon_script.py`. Duas exceções ao corpo por extenso:

* **Arquivo** (o app e as bibliotecas): vai o sha256, tirado com as quebras de
  linha normalizadas — no Windows o Git as troca no checkout, e o Python lê o
  HTML em modo texto.
* **422 de validação** vira `<validação>`. O texto é do pydantic, uma lista de
  dicionários que o app do garçom nunca lê (ele mostra "Erro 422"); exigir o
  mesmo texto do C# seria portar o pydantic. O status é conferido, e o 422 que
  a rota monta à mão (quantidade ilegível) continua por extenso.

Passo com `op` não é requisição: é o que a pessoa faz no caixa (gerar o código
de pareamento, revogar um aparelho).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import salon_script
from salon_script import CAFE, FATIA, MANAGER, MISSING, TORTA, _uuid

from pdv.edge.auth import EdgeAuth
from pdv.edge.hub import EventHub

CONTRACT = salon_script.CONTRACTS / "salon-http.json"
STORE_NAME = "Confeitaria Aurora"

#: Os cabeçalhos da resposta que o app lê ou que mudam o comportamento dele.
KEPT_HEADERS = ("x-auth-scope", "cache-control")


def _pairs(n: int) -> list[dict[str, Any]]:
    return [
        {"method": "POST", "path": "/pair", "json": {"code": f"9999000{i}", "device_name": "Ataque"}}
        for i in range(n)
    ]


SCRIPT: list[dict[str, Any]] = [
    # -- aberto --------------------------------------------------------------- #
    {"method": "GET", "path": "/health"},
    {"method": "GET", "path": "/manifest.webmanifest"},
    {"method": "GET", "path": "/"},
    {"method": "GET", "path": "/vendor/sweetalert2.min.css"},
    {"method": "GET", "path": "/vendor/sweetalert2.min.js"},
    {"method": "GET", "path": "/vendor/nao-existe.js"},
    {"method": "GET", "path": "/rota-que-nao-existe"},
    {"method": "DELETE", "path": "/health"},
    # -- pareamento ------------------------------------------------------------- #
    {"method": "GET", "path": "/tables"},
    {"method": "GET", "path": "/tables", "headers": {"device": "token-inventado"}},
    {"method": "GET", "path": "/tables", "raw_headers": {"Authorization": "Basic abc"}},
    {"op": "code", "save": "c1"},
    {"method": "POST", "path": "/pair", "json": {"code": "$c1", "device_name": "Celular do João"},
     "save": {"tok1": "token", "dev1": "device_id"}},
    {"method": "POST", "path": "/pair", "json": {"code": "$c1", "device_name": "Outro"}},
    {"method": "POST", "path": "/pair", "json": {"code": "12", "device_name": "Curto"}},
    {"method": "POST", "path": "/pair", "json": {"code": "12345678", "device_name": "TV", "kind": "tv"}},
    {"method": "POST", "path": "/pair", "json": {"code": "12345678"}},
    {"method": "POST", "path": "/pair", "raw": "{"},
    {"op": "code", "save": "c2"},
    {"method": "POST", "path": "/pair", "json": {"code": "$c2", "device_name": "Tablet da cozinha", "kind": "kds"},
     "save": {"tok2": "token", "dev2": "device_id"}},
    # -- leitura, só com o aparelho ------------------------------------------- #
    {"method": "GET", "path": "/tables", "headers": {"device": "$tok1"}},
    {"method": "GET", "path": "/menu", "headers": {"device": "$tok1"}},
    {"method": "GET", "path": "/orders", "headers": {"device": "$tok1"}},
    # -- sessão do garçom -------------------------------------------------------- #
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1"},
     "json": {"client_uuid": _uuid(0xc101), "table_label": "Mesa 1"}},
    # A ordem de conferência: sem sessão, o corpo incompleto nem é olhado...
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1"}, "json": {"client_uuid": "x"}},
    # ...mas JSON ilegível é recusado antes até do aparelho.
    {"method": "POST", "path": "/staff/session", "raw": "{\"login\": "},
    {"method": "POST", "path": "/staff/session", "headers": {"device": "$tok1"},
     "json": {"login": "joao", "pin": "0000"}},
    {"method": "POST", "path": "/staff/session", "json": {"login": "joao", "pin": "4826"}},
    {"method": "POST", "path": "/staff/session", "headers": {"device": "$tok1"},
     "json": {"login": "joao", "pin": "4826"}, "save": {"st1": "token"}},
    {"method": "GET", "path": "/staff/session", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "GET", "path": "/staff/session", "headers": {"device": "$tok2", "staff": "$st1"}},
    {"method": "GET", "path": "/staff/session", "headers": {"device": "$tok1"}},
    # -- gerente ------------------------------------------------------------------ #
    {"method": "POST", "path": "/tables/seed", "headers": {"device": "$tok1"}, "json": {"count": 3}},
    {"method": "POST", "path": "/manager/session", "headers": {"device": "$tok1"},
     "json": {"login": "bruno", "pin": "1111"}},
    {"method": "POST", "path": "/manager/session", "headers": {"device": "$tok1"},
     "json": {"login": "joao", "pin": "4826"}},
    {"method": "POST", "path": "/manager/session", "headers": {"device": "$tok1"},
     "json": {"login": "bruno", "pin": "7391"}, "save": {"mg1": "token"}},
    {"method": "GET", "path": "/manager/session", "headers": {"device": "$tok1", "manager": "$mg1"}},
    {"method": "GET", "path": "/manager/session", "headers": {"device": "$tok2", "manager": "$mg1"}},
    # -- mesas ---------------------------------------------------------------------- #
    {"method": "POST", "path": "/tables/seed", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"count": 3}},
    {"method": "POST", "path": "/tables/seed", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"count": 0}},
    {"method": "POST", "path": "/tables", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "Varanda 1", "area": "Varanda", "seats": 2}, "save": {"v1": "id"}},
    {"method": "POST", "path": "/tables", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "mesa 1"}},
    {"method": "POST", "path": "/tables", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "Sem lugar", "seats": 0}},
    {"method": "POST", "path": "/tables", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "   "}},
    {"method": "PATCH", "path": "/tables/$v1", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "Varanda 9", "seats": 6}},
    {"method": "PATCH", "path": "/tables/$v1", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"is_active": False, "label": "Varanda 10"}},
    {"method": "PATCH", "path": "/tables/$v1", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "Varanda 11"}},
    {"method": "PATCH", "path": "/tables/$v1", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"is_active": True, "sort_order": 50}},
    {"method": "PATCH", "path": f"/tables/{MISSING}", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"label": "Y"}},
    {"method": "GET", "path": "/tables?include_inactive=true", "headers": {"device": "$tok1"}},
    {"method": "GET", "path": "/tables", "headers": {"device": "$tok1"},
     "save": {"m1": "tables.0.id", "m2": "tables.1.id", "m3": "tables.2.id"}},
    # -- comandas --------------------------------------------------------------------- #
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc102), "table_id": "$m1"}, "save": {"o1": "order_id"}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc102), "table_id": "$m1"}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc103), "table_id": "$m1"}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc104), "table_label": "Mesa 99"}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc105)}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": "curto"}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd101), "product_id": CAFE, "quantity": "2", "notes": "sem açúcar"}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd101), "product_id": CAFE, "quantity": "2"}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd102), "product_id": CAFE, "quantity": "abc"}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd103), "product_id": TORTA}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd104), "product_id": MISSING}},
    {"method": "POST", "path": f"/orders/{MISSING}/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd105), "product_id": CAFE}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd106), "product_id": FATIA, "station": "confeitaria"}},
    {"method": "GET", "path": "/orders", "headers": {"device": "$tok1"}},
    {"method": "GET", "path": "/orders/$o1", "headers": {"device": "$tok1"}},
    {"method": "GET", "path": f"/orders/{MISSING}", "headers": {"device": "$tok1"}},
    {"method": "POST", "path": "/orders/$o1/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "POST", "path": "/orders/$o1/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "GET", "path": "/tables", "headers": {"device": "$tok1"}},
    {"method": "DELETE", "path": "/orders/$o1/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "POST", "path": f"/orders/{MISSING}/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    # -- cozinha -------------------------------------------------------------------------- #
    {"method": "GET", "path": "/kds/tickets", "headers": {"device": "$tok2"},
     "save": {"t1": "tickets.0.ticket_id", "t2": "tickets.1.ticket_id"}},
    {"method": "GET", "path": "/kds/tickets?station=confeitaria", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t1/bump", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t1/bump", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t1/recall", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t1/explodir", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": f"/kds/tickets/{MISSING}/bump", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t2/recall", "headers": {"device": "$tok2"}},
    {"method": "POST", "path": "/kds/tickets/$t2/bump"},
    # -- transferir e cancelar -------------------------------------------------------------- #
    {"method": "POST", "path": "/orders/$o1/transfer", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"table_id": "$m2"}},
    {"method": "POST", "path": "/orders/$o1/transfer", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"table_id": "$m2"}},
    {"method": "POST", "path": "/orders", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xc106), "table_label": "mesa 1"}, "save": {"o2": "order_id"}},
    {"method": "POST", "path": "/orders/$o1/transfer", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"table_id": "$m1"}},
    {"method": "POST", "path": "/orders/$o1/transfer", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"table_id": MISSING}},
    {"method": "POST", "path": "/orders/$o2/cancel", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"reason": "ab"}},
    {"method": "POST", "path": "/orders/$o2/cancel", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"reason": "aberta por engano"}},
    {"method": "POST", "path": "/orders/$o2/cancel", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"reason": "  aberta   por engano "}},
    {"method": "POST", "path": "/orders/$o2/cancel", "headers": {"device": "$tok1", "staff": "$st1", "manager": "$mg1"},
     "json": {"reason": "de novo"}},
    {"method": "POST", "path": "/orders/$o2/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "DELETE", "path": "/orders/$o2/bill", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "POST", "path": "/orders/$o2/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd107), "product_id": CAFE}},
    {"method": "GET", "path": "/kds/tickets", "headers": {"device": "$tok2"}},
    # -- turno e saída ---------------------------------------------------------------------------- #
    {"method": "GET", "path": "/staff/summary", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "DELETE", "path": "/staff/session", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "DELETE", "path": "/staff/session", "headers": {"device": "$tok1", "staff": "$st1"}},
    {"method": "DELETE", "path": "/staff/session", "headers": {"device": "$tok1"}},
    {"method": "POST", "path": "/orders/$o1/items", "headers": {"device": "$tok1", "staff": "$st1"},
     "json": {"client_uuid": _uuid(0xd108), "product_id": CAFE}},
    {"method": "DELETE", "path": "/manager/session", "headers": {"device": "$tok1", "manager": "$mg1"}},
    {"method": "DELETE", "path": "/manager/session", "headers": {"device": "$tok1", "manager": "$mg1"}},
    {"method": "GET", "path": "/manager/session", "headers": {"device": "$tok1", "manager": "$mg1"}},
    {"method": "PATCH", "path": "/tables/$v1", "headers": {"device": "$tok1", "manager": "$mg1"},
     "json": {"is_active": False}},
    # -- aparelho revogado e o freio do pareamento -------------------------------------------------- #
    {"op": "revoke", "device": "$dev2"},
    {"method": "GET", "path": "/kds/tickets", "headers": {"device": "$tok2"}},
    *_pairs(11),
    {"op": "code", "save": "c3"},
    {"method": "POST", "path": "/pair", "json": {"code": "$c3", "device_name": "Legítimo"}},
]


def _dig(value: Any, path: str) -> str:
    for part in path.split("."):
        value = value[int(part)] if part.isdigit() else value[part]
    return str(value)


def _file_digest(body: bytes) -> dict[str, Any]:
    text = body.replace(b"\r\n", b"\n")
    return {"sha256": hashlib.sha256(text).hexdigest(), "bytes": len(text)}


def _outcome(response: Any) -> dict[str, Any]:
    media = response.headers.get("content-type", "").split(";")[0].strip()
    outcome: dict[str, Any] = {"status": response.status_code, "type": media}
    headers = {k: response.headers[k] for k in KEPT_HEADERS if k in response.headers}
    if headers:
        outcome["headers"] = headers
    if media == "application/json":
        body = response.json()
        if (
            response.status_code == 422
            and isinstance(body, dict)
            and isinstance(body.get("detail"), list)
        ):
            body = "<validação>"
        outcome["body"] = body
    else:
        outcome["body"] = _file_digest(response.content)
    return outcome


def run(tmp_path: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from pdv.edge.server import create_app

    database, config = salon_script.open_database(tmp_path, store_name=STORE_NAME)
    hub = EventHub()
    auth = EdgeAuth(database, salon_script.TENANT, salon_script.STORE)
    saved: dict[str, str] = {}

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            for name in sorted(saved, key=len, reverse=True):
                value = value.replace(f"${name}", saved[name])
            return value
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    normalizer = salon_script.Normalizer()
    results = []
    with TestClient(create_app(database, config, hub)) as client:
        for step in SCRIPT:
            if step.get("op") == "code":
                code, _ = auth.create_pairing_code()
                saved[step["save"]] = code
                results.append({"done": step["op"]})
                continue
            if step.get("op") == "revoke":
                results.append({"done": step["op"], "result": auth.revoke(resolve(step["device"]))})
                continue

            headers = {}
            names = resolve(step.get("headers", {}))
            if "device" in names:
                headers["Authorization"] = f"Bearer {names['device']}"
            if "staff" in names:
                headers["X-Staff-Token"] = names["staff"]
            if "manager" in names:
                headers["X-Manager-Token"] = names["manager"]
            headers.update(step.get("raw_headers", {}))
            kwargs: dict[str, Any] = {"headers": headers}
            if "json" in step:
                kwargs["json"] = resolve(step["json"])
            if "raw" in step:
                kwargs["content"] = step["raw"]
                headers["Content-Type"] = "application/json"

            response = client.request(step["method"], resolve(step["path"]), **kwargs)
            outcome = _outcome(response)
            for name, path in (step.get("save") or {}).items():
                saved[name] = _dig(outcome["body"], path)
            results.append(normalizer.value(outcome))

    database.close()
    return {"results": results}


def document(run_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "descricao": (
            "As rotas do servidor do salão como o FastAPI as responde. Gerado por "
            "apps/desktop-pdv/tests/salon_http_script.py; não edite à mão: rode "
            "PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_salon_http_contract.py."
        ),
        "tenant_id": salon_script.TENANT,
        "store_id": salon_script.STORE,
        "device_id": salon_script.DEVICE,
        "store_name": STORE_NAME,
        "device_secret": salon_script.DEVICE_SECRET,
        "manager_id": MANAGER,
        "seed": salon_script.SEED,
        "script": SCRIPT,
        **run_result,
    }
