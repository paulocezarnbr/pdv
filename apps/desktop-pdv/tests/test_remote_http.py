"""O `HttpTransport` contra um servidor **de verdade**.

`test_remote_transport.py` usa uma nuvem falsa e prova a lógica: a ordem dos
passos, o que acontece quando cada um falha. Ele não prova o formato do fio —
nome de parâmetro, forma do JSON, código de status. Um transporte que monta
`?device=` onde o servidor espera `?device_id=` passa em todos aqueles testes.

Esse é exatamente o defeito que custou caro na Fase 3: serviços 100% verdes e
**toda** rota respondendo 422. A lição virou este arquivo.

O servidor aqui é um dublê mínimo — só as duas rotas, com a mesma forma de
payload do `cloud-api`. Subir o `cloud-api` inteiro exigiria banco e middleware
de tenancy, e o que precisa ser verificado é o contrato entre as duas pontas,
não a persistência da nuvem.
"""

from __future__ import annotations

import json

import pytest

from pdv.remote.inbox import CommandResult
from pdv.remote.protocol import CommandKind, CommandStatus, RemoteCommand, sign_command
from pdv.sync.protocol import (
    AuthError,
    CommandFetch,
    CommandReport,
    TransportError,
)
from pdv.sync.transport import HttpTransport

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

DEVICE = "33333333-3333-3333-3333-333333333333"
TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
SECRET = b"segredo-do-terminal"


@pytest.fixture()
def cloud():  # noqa: ANN201
    """Um dublê da nuvem, com as duas rotas de comando."""
    from fastapi import FastAPI, Header, HTTPException, Query
    from fastapi.testclient import TestClient

    app = FastAPI()
    state: dict[str, object] = {"commands": [], "received": None, "accept_all": True}

    @app.get("/commands/pending")
    async def pending(  # noqa: ANN202
        tenant_id: str = Query(),
        store_id: str = Query(),
        device_id: str = Query(),
        limit: int = Query(50),
        authorization: str | None = Header(default=None),
    ):
        if authorization != "Bearer token-do-terminal":
            raise HTTPException(401, "sem token")
        state["last_query"] = {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "device_id": device_id,
            "limit": limit,
        }
        return {"commands": state["commands"][:limit]}

    @app.post("/commands/results")
    async def results(body: dict, authorization: str | None = Header(default=None)):  # noqa: ANN202
        if authorization != "Bearer token-do-terminal":
            raise HTTPException(401, "sem token")
        state["received"] = body
        accepted = [r["command_uuid"] for r in body["results"]]
        return {"accepted": accepted if state["accept_all"] else []}

    @app.get("/boom")
    async def boom():  # noqa: ANN202
        raise HTTPException(500, "servidor caiu")

    # O `HttpTransport` normalmente abre o próprio cliente httpx e põe o
    # `Authorization` nele. Aqui ele recebe o do TestClient, que fala com o app
    # em processo — sem porta e sem socket —, então o cabeçalho é montado do
    # mesmo jeito à mão. É a única costura entre teste e produção neste arquivo.
    client = TestClient(app, headers={"Authorization": "Bearer token-do-terminal"})
    transport = HttpTransport("http://testserver", "token-do-terminal")
    transport._client = client
    return transport, state, client


def _wire_command(percent: str = "25") -> dict:
    """Um comando no formato exato que o `cloud-api` devolve."""
    payload = {"order_id": "pedido-1", "percent": percent, "reason": "Atraso"}
    issued_at = "2026-09-19T12:00:00+00:00"
    return {
        "command_uuid": "cmd-1",
        "tenant_id": TENANT,
        "store_id": STORE,
        "device_id": DEVICE,
        "kind": "apply_discount",
        "payload": payload,
        "issued_by_user_id": "gerente-1",
        "issued_by_name": "Bruno Gerente",
        "issued_at": issued_at,
        "signature": sign_command(
            secret=SECRET,
            command_uuid="cmd-1",
            device_id=DEVICE,
            kind="apply_discount",
            payload=payload,
            issued_at=issued_at,
        ),
    }


# --------------------------------------------------------------------------- #
# Busca
# --------------------------------------------------------------------------- #


def test_the_query_carries_the_identity_the_server_expects(cloud) -> None:  # noqa: ANN001
    """Os nomes dos parâmetros. É aqui que o 422 silencioso nasce."""
    transport, state, _ = cloud

    transport.fetch_commands(
        CommandFetch(tenant_id=TENANT, store_id=STORE, device_id=DEVICE, limit=7)
    )

    assert state["last_query"] == {
        "tenant_id": TENANT,
        "store_id": STORE,
        "device_id": DEVICE,
        "limit": 7,
    }


def test_a_command_survives_the_round_trip_intact(cloud) -> None:  # noqa: ANN001
    """E, crucialmente, com a assinatura ainda válida.

    Qualquer reordenação de chave no caminho quebraria a conferência no
    terminal — que é o modo de falha que este arquivo existe para pegar.
    """
    transport, state, _ = cloud
    state["commands"] = [_wire_command()]

    delivery = transport.fetch_commands(
        CommandFetch(tenant_id=TENANT, store_id=STORE, device_id=DEVICE)
    )

    assert len(delivery.commands) == 1
    command = delivery.commands[0]
    assert isinstance(command, RemoteCommand)
    assert command.kind is CommandKind.APPLY_DISCOUNT
    assert command.issued_by_name == "Bruno Gerente"

    from pdv.remote.protocol import verify_signature

    assert verify_signature(command, SECRET), "a assinatura sobreviveu ao JSON"


def test_a_malformed_command_does_not_block_the_good_ones(cloud) -> None:  # noqa: ANN001
    """Uma entrada estranha no meio do lote não pode segurar o resto.

    O que ela perde é a chance de ser aplicada — e esse é o resultado certo:
    comando que não se consegue nem ler não se obedece.
    """
    transport, state, _ = cloud
    state["commands"] = [
        {"command_uuid": "sem-o-resto"},
        _wire_command(),
        {**_wire_command(), "kind": "abrir_gaveta"},  # nuvem mais nova
    ]

    delivery = transport.fetch_commands(
        CommandFetch(tenant_id=TENANT, store_id=STORE, device_id=DEVICE)
    )

    assert len(delivery.commands) == 1
    assert delivery.commands[0].command_uuid == "cmd-1"


def test_an_unknown_kind_is_dropped_not_guessed(cloud) -> None:  # noqa: ANN001
    """A nuvem pode ser mais nova que o PDV.

    Diante de uma ordem que não se entende, o comportamento seguro é não
    obedecer — nunca adivinhar qual seria a mais parecida.
    """
    transport, state, _ = cloud
    state["commands"] = [{**_wire_command(), "kind": "transferir_fundos"}]

    assert transport.fetch_commands(
        CommandFetch(tenant_id=TENANT, store_id=STORE, device_id=DEVICE)
    ).commands == ()


# --------------------------------------------------------------------------- #
# Relato
# --------------------------------------------------------------------------- #


def test_the_report_body_has_the_shape_the_server_reads(cloud) -> None:  # noqa: ANN001
    transport, state, _ = cloud

    accepted = transport.report_commands(
        CommandReport(
            tenant_id=TENANT,
            store_id=STORE,
            device_id=DEVICE,
            results=(
                CommandResult("cmd-1", CommandStatus.APPLIED, "aplicado", "2026-09-19T12:00:01+00:00"),
                CommandResult("cmd-2", CommandStatus.REFUSED, "acima do teto", "2026-09-19T12:00:02+00:00"),
            ),
        )
    )

    body = state["received"]
    assert body["device_id"] == DEVICE
    assert [r["status"] for r in body["results"]] == ["applied", "refused"]
    assert body["results"][1]["message"] == "acima do teto"
    assert accepted == ("cmd-1", "cmd-2")


def test_an_empty_confirmation_clears_nothing(cloud) -> None:  # noqa: ANN001
    """"Não confirmei nada" precisa chegar como tupla vazia, não como sucesso."""
    transport, state, _ = cloud
    state["accept_all"] = False

    accepted = transport.report_commands(
        CommandReport(
            tenant_id=TENANT,
            store_id=STORE,
            device_id=DEVICE,
            results=(CommandResult("cmd-1", CommandStatus.APPLIED, "", ""),),
        )
    )

    assert accepted == ()


# --------------------------------------------------------------------------- #
# Erros do servidor
# --------------------------------------------------------------------------- #


def test_a_revoked_token_is_an_auth_error(cloud) -> None:  # noqa: ANN001
    """401 não é retentável: reenviar com a mesma credencial dá o mesmo 401."""
    transport, _, client = cloud
    client.headers["Authorization"] = "Bearer token-errado"
    transport._client = client

    with pytest.raises(AuthError):
        transport.fetch_commands(
            CommandFetch(tenant_id=TENANT, store_id=STORE, device_id=DEVICE)
        )


def test_a_server_error_is_retryable(cloud) -> None:  # noqa: ANN001
    """500 pode ter acontecido depois do efeito. Na dúvida, tenta de novo."""
    transport, _, client = cloud

    with pytest.raises(TransportError):
        transport._body(client.get("/boom"))


def test_an_unreadable_body_is_a_transport_error(cloud) -> None:  # noqa: ANN001
    """Resposta ilegível nunca vira "recebi zero comandos" em silêncio."""
    transport, _, _ = cloud

    class NotJson:
        status_code = 200
        text = "<html>proxy do hotel</html>"

        def json(self):  # noqa: ANN202
            raise json.JSONDecodeError("nope", "", 0)

    with pytest.raises(TransportError, match="ilegível"):
        transport._body(NotJson())
