"""O cano que entrega os comandos — Fase 3.5.b, transporte.

`test_remote_commands.py` prova que o terminal aplica e recusa direito. Este
arquivo prova o que está em volta: buscar, gravar na inbox, aplicar e relatar,
na ordem certa e falhando para o lado certo em cada passo.

Os testes de rede são escritos contra um transporte falso, e não contra HTTP
real, pelo mesmo motivo registrado em `sync/protocol.py`: o comportamento que
importa é o das **falhas específicas** — a resposta que se perde depois de o
efeito ter acontecido, a confirmação parcial, a nuvem que não fala de comando.
Nenhuma delas se reproduz de forma confiável contra rede de verdade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_MANAGER_ID, DEMO_MANAGER_NAME, DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import EntityId, iso, new_id, utc_now
from pdv.remote.commands import RemoteCommandService
from pdv.remote.inbox import InboxRepository
from pdv.remote.protocol import CommandKind, CommandStatus, RemoteCommand, sign_command
from pdv.services.checkout import CheckoutService
from pdv.sync.engine import SyncEngine
from pdv.sync.protocol import (
    AuthError,
    CommandDelivery,
    CommandFetch,
    CommandReport,
    PullResponse,
    PushResponse,
    TransportError,
    speaks_commands,
)

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
SECRET = b"segredo-do-terminal-provisionado-na-ativacao"


# --------------------------------------------------------------------------- #
# Cenário
# --------------------------------------------------------------------------- #


class DataOnlyTransport:
    """Uma nuvem que só sabe sincronizar dados — a de antes desta fase."""

    def push(self, batch):  # noqa: ANN001, ANN201
        return PushResponse(acks=())

    def pull(self, request):  # noqa: ANN001, ANN201
        return PullResponse(entity_table=request.entity_table, rows=(), last_server_seq=0)


class FakeCloud(DataOnlyTransport):
    """Uma nuvem que fala de comando, e que sabe falhar sob encomenda."""

    def __init__(self) -> None:
        self.queue: list[RemoteCommand] = []
        self.reported: list[CommandReport] = []
        self.fetches = 0
        self.fail_fetch: Exception | None = None
        self.fail_report: Exception | None = None
        #: Quais uuids confirmar no relato. `None` = todos.
        self.confirm: list[str] | None = None

    def fetch_commands(self, request: CommandFetch) -> CommandDelivery:
        self.fetches += 1
        if self.fail_fetch is not None:
            raise self.fail_fetch
        # Entrega não consome: repetir a entrega é o caso normal de uma rede
        # instável, e o terminal deduplica por `command_uuid`.
        return CommandDelivery(commands=tuple(self.queue[: request.limit]))

    def report_commands(self, report: CommandReport) -> tuple[str, ...]:
        if self.fail_report is not None:
            raise self.fail_report
        self.reported.append(report)
        settled = {r.command_uuid for r in report.results}
        # Uma nuvem real para de entregar o que já foi relatado.
        self.queue = [c for c in self.queue if c.command_uuid not in settled]
        if self.confirm is None:
            return tuple(settled)
        return tuple(u for u in self.confirm if u in settled)


@pytest.fixture()
def terminal(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
        device_secret=SECRET,
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)

    checkout = CheckoutService(database, config)
    sale = checkout.open_sale(EntityId(DEMO_OPERATOR_ID))
    product = next(
        p
        for p in ProductRepository(database.connection).list_active(EntityId(TENANT))
        if p.sku == "CAFE-EXP"
    )
    from decimal import Decimal

    for _ in range(4):  # R$ 28,00
        checkout.register_unit_item(
            product=product,
            quantity=Decimal("1"),
            operator_id=EntityId(DEMO_OPERATOR_ID),
        )

    cloud = FakeCloud()
    engine = SyncEngine(
        database,
        cloud,
        config,
        commands=RemoteCommandService(database, config, checkout=checkout),
    )
    return database, config, cloud, engine, sale


def _discount(order_id: str, percent: str = "25", *, device_id: str = DEVICE) -> RemoteCommand:
    payload = {
        "order_id": order_id,
        "percent": percent,
        "reason": "Cliente aguardou 40 minutos",
    }
    command_uuid = new_id()
    issued_at = iso(utc_now())
    return RemoteCommand(
        command_uuid=command_uuid,
        tenant_id=TENANT,
        store_id=STORE,
        device_id=device_id,
        kind=CommandKind.APPLY_DISCOUNT,
        payload=payload,
        issued_by_user_id=DEMO_MANAGER_ID,
        issued_by_name=DEMO_MANAGER_NAME,
        issued_at=issued_at,
        signature=sign_command(
            secret=SECRET,
            command_uuid=command_uuid,
            device_id=device_id,
            kind=CommandKind.APPLY_DISCOUNT.value,
            payload=payload,
            issued_at=issued_at,
        ),
    )


def _discount_cents(database: Database, order_id: str) -> int:
    row = database.query_one("SELECT discount_cents FROM orders WHERE id = ?", (order_id,))
    return int(row["discount_cents"])


# --------------------------------------------------------------------------- #
# O ciclo completo
# --------------------------------------------------------------------------- #


def test_the_command_travels_from_the_panel_to_the_receipt(terminal) -> None:  # noqa: ANN001
    """O caminho inteiro: buscar, gravar, aplicar, relatar."""
    database, _, cloud, engine, sale = terminal
    command = _discount(str(sale.id))
    cloud.queue.append(command)

    report = engine.command_cycle()

    assert (report.fetched, report.accepted, report.applied) == (1, 1, 1)
    assert report.reported == 1
    assert _discount_cents(database, str(sale.id)) == 700
    assert InboxRepository(database).status_of(command.command_uuid) is CommandStatus.APPLIED


def test_a_cloud_that_does_not_speak_commands_is_not_a_failure(terminal, tmp_path) -> None:  # noqa: ANN001
    """Nuvem antiga continua servindo terminal novo.

    O canal de comando não pode virar requisito para a venda subir — que é a
    única coisa aqui que não pode parar.
    """
    database, config, _, _, sale = terminal
    engine = SyncEngine(
        database,
        DataOnlyTransport(),
        config,
        commands=RemoteCommandService(database, config),
    )

    assert speaks_commands(DataOnlyTransport()) is False
    assert engine.speaks_commands is False

    report = engine.command_cycle()

    assert (report.fetched, report.applied) == (0, 0)
    assert report.error is None, "não falar de comando não é falha"
    assert engine.push_once().error is None, "e a venda continua subindo"


def test_a_terminal_without_the_service_does_not_obey(terminal) -> None:  # noqa: ANN001
    """Sem `commands=`, o terminal só envia — como era até a Fase 3.5."""
    database, config, cloud, _, sale = terminal
    cloud.queue.append(_discount(str(sale.id)))
    engine = SyncEngine(database, cloud, config)

    assert engine.speaks_commands is False
    assert engine.command_cycle().fetched == 0
    assert cloud.fetches == 0
    assert _discount_cents(database, str(sale.id)) == 0


# --------------------------------------------------------------------------- #
# Falhas de rede, uma por passo
# --------------------------------------------------------------------------- #


def test_a_failed_fetch_has_no_effect_at_all(terminal) -> None:  # noqa: ANN001
    """Falhar ao buscar aborta o ciclo sem meio-estado."""
    database, _, cloud, engine, sale = terminal
    cloud.queue.append(_discount(str(sale.id)))
    cloud.fail_fetch = TransportError("rede caiu")

    report = engine.command_cycle()

    assert report.error is not None
    assert (report.accepted, report.applied) == (0, 0)
    assert InboxRepository(database).pending_count() == 0
    assert _discount_cents(database, str(sale.id)) == 0


def test_a_revoked_device_does_not_stop_the_sale_queue(terminal) -> None:  # noqa: ANN001
    """Credencial revogada é erro do canal de comando, não do terminal."""
    _, _, cloud, engine, sale = terminal
    cloud.queue.append(_discount(str(sale.id)))
    cloud.fail_fetch = AuthError("terminal revogado")

    report = engine.command_cycle()

    assert "revogado" in (report.error or "")
    # E o push segue funcionando: o ciclo de comando não contamina o de dados.
    assert engine.push_once().error is None


def test_a_failed_report_never_undoes_what_was_applied(terminal) -> None:  # noqa: ANN001
    """O passo que não pode ser invertido.

    Avisar a nuvem é operação de rede e pode falhar. Falhar ao avisar não pode
    desfazer o desconto — nem autorizar uma segunda aplicação.
    """
    database, _, cloud, engine, sale = terminal
    command = _discount(str(sale.id))
    cloud.queue.append(command)
    cloud.fail_report = TransportError("rede caiu no relato")

    report = engine.command_cycle()

    assert report.applied == 1
    assert report.reported == 0
    assert report.error is not None
    assert _discount_cents(database, str(sale.id)) == 700

    # O resultado continua na fila de relato, e o próximo ciclo reavisa.
    cloud.fail_report = None
    second = engine.command_cycle()

    assert second.reported == 1
    assert second.applied == 0, "reavisar não reaplica"
    assert _discount_cents(database, str(sale.id)) == 700


def test_a_partial_confirmation_only_clears_what_the_cloud_named(terminal) -> None:  # noqa: ANN001
    """Confirmação em bloco esconderia gravação parcial.

    O painel ficaria mostrando `pendente` num comando já aplicado — que é o
    estado em que alguém reemite o desconto na mão.
    """
    database, _, cloud, engine, sale = terminal
    first = _discount(str(sale.id), "10")
    second = _discount(str(sale.id), "20")
    cloud.queue.extend([first, second])
    cloud.confirm = [first.command_uuid]  # a nuvem gravou só um

    engine.command_cycle()

    unreported = [r.command_uuid for r in InboxRepository(database).unreported()]
    assert unreported == [second.command_uuid]


def test_a_cloud_inventing_uuids_does_not_clear_the_queue(terminal) -> None:  # noqa: ANN001
    """Só se tira da fila o que foi relatado nesta rodada.

    Uma nuvem defeituosa (ou hostil) que responda uuids aleatórios não pode
    fazer o terminal esquecer resultados que ela nunca recebeu.
    """
    database, _, cloud, engine, sale = terminal
    command = _discount(str(sale.id))
    cloud.queue.append(command)
    cloud.confirm = [new_id(), new_id()]

    engine.command_cycle()

    assert [r.command_uuid for r in InboxRepository(database).unreported()] == [
        command.command_uuid
    ]


# --------------------------------------------------------------------------- #
# Reentrega
# --------------------------------------------------------------------------- #


def test_redelivery_before_the_report_lands_does_not_double_the_discount(terminal) -> None:  # noqa: ANN001
    """O caso que a entrega não-destrutiva cria de propósito.

    A nuvem reentrega até o terminal relatar. Entre aplicar e relatar, a mesma
    ordem chega de novo — e precisa virar no-op.
    """
    database, _, cloud, engine, sale = terminal
    command = _discount(str(sale.id))
    cloud.queue.append(command)
    cloud.fail_report = TransportError("o relato não chegou")

    engine.command_cycle()
    second = engine.command_cycle()  # a nuvem reentrega o mesmo comando

    assert second.fetched == 1, "a nuvem ainda não sabe e reentrega"
    assert second.accepted == 0, "o command_uuid colide na inbox"
    assert second.applied == 0
    assert _discount_cents(database, str(sale.id)) == 700


def test_a_refusal_is_reported_with_its_reason(terminal) -> None:  # noqa: ANN001
    """O painel precisa dizer **por que** não foi aplicado.

    Um "recusado" sem motivo faz o gerente tentar de novo igual.
    """
    database, _, cloud, engine, sale = terminal
    cloud.queue.append(_discount(str(sale.id), "80"))  # o teto de Bruno é 30%

    report = engine.command_cycle()

    assert (report.applied, report.refused) == (0, 1)
    sent = cloud.reported[0].results[0]
    assert sent.status is CommandStatus.REFUSED
    assert "30" in sent.message and "80" in sent.message
    assert _discount_cents(database, str(sale.id)) == 0


def test_a_command_for_another_terminal_is_refused_and_reported(terminal) -> None:  # noqa: ANN001
    """Erro de roteamento na nuvem não vira desconto no terminal errado."""
    database, _, cloud, engine, sale = terminal
    cloud.queue.append(
        _discount(str(sale.id), device_id="99999999-9999-9999-9999-999999999999")
    )

    report = engine.command_cycle()

    assert report.refused == 1
    assert _discount_cents(database, str(sale.id)) == 0
    entry = database.query_one(
        "SELECT severity FROM audit_ledger "
        " WHERE event_type = 'remote_command_refused' ORDER BY seq DESC"
    )
    assert entry["severity"] == "critical"


# --------------------------------------------------------------------------- #
# Contrato com a nuvem
# --------------------------------------------------------------------------- #


def _ask_the_cloud(payload: dict, *, issued_at: str = "2026-09-19T12:00:00+00:00"):
    """Executa a assinatura **da nuvem** e devolve o que ela calculou.

    A nuvem virou TypeScript; o terminal continua em Python. Comparar de
    verdade exige **executar** as duas — reimplementar uma na outra linguagem
    dentro do teste seria comparar uma cópia com outra cópia, e é justamente a
    divergência entre as implementações reais que este teste existe para pegar.

    A ponte é `scripts/sign.ts`, que a nuvem mantém para este fim.
    """
    import json
    import shutil
    import subprocess

    script = (
        Path(__file__).resolve().parents[2] / "cloud-api" / "scripts" / "sign.ts"
    )
    if not script.exists():  # pragma: no cover
        pytest.skip("cloud-api não está neste checkout")

    node = shutil.which("node")
    if node is None:  # pragma: no cover - ambiente sem Node
        pytest.skip("Node não está instalado neste ambiente")

    request = json.dumps(
        {
            "secret_hex": SECRET.hex(),
            "command_uuid": "cmd-1",
            "device_id": DEVICE,
            "kind": "apply_discount",
            "payload": payload,
            "issued_at": issued_at,
        },
        ensure_ascii=False,
    )

    result = subprocess.run(
        [node, "--experimental-strip-types", str(script)],
        input=request.encode("utf-8"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover
        pytest.fail(
            "a assinatura da nuvem não rodou:\n"
            + result.stderr.decode("utf-8", "replace")
        )

    # O Node escreve avisos no stderr; a resposta é a última linha do stdout.
    return json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])


def test_both_sides_compute_the_same_signature() -> None:
    """O teste que impede a sexta-feira à noite.

    A nuvem assina e o terminal confere, com implementações separadas — agora
    em **linguagens diferentes**, o que torna a divergência mais provável, não
    menos. Duas versões do mesmo HMAC divergem em algum detalhe de
    serialização — ordem de chave, espaço, acento escapado — e a divergência
    aparece como "o painel parou de funcionar", sem nada no log dizendo por quê.

    Aqui as duas são executadas de verdade e comparadas byte a byte. Quem mexer
    numa quebra o CI, não a loja.
    """
    # Payloads escolhidos para pegar justamente o que costuma divergir: ordem
    # das chaves, acento, tipo numérico, string vazia e aninhamento.
    for payload in (
        {},
        {"order_id": "abc", "percent": "12.5", "reason": "Atraso na cozinha"},
        {"z": 1, "a": 2, "m": "ç ã é", "vazio": ""},
        {"aninhado": {"b": 2, "a": [3, 1, 2]}},
    ):
        cloud = _ask_the_cloud(payload)
        mine = sign_command(
            secret=SECRET,
            command_uuid="cmd-1",
            device_id=DEVICE,
            kind="apply_discount",
            payload=payload,
            issued_at="2026-09-19T12:00:00+00:00",
        )
        assert cloud["signature"] == mine, payload

    from pdv.remote.protocol import canonical_payload

    # E o texto canônico também, não só o hash: um hash igual por acaso com
    # textos diferentes é improvável, mas comparar o texto diz *onde* divergiu
    # quando a divergência aparecer.
    nested = {"b": 1, "a": "ç"}
    assert _ask_the_cloud(nested)["canonical_payload"] == canonical_payload(nested)


def test_the_arrays_keep_their_order_on_both_sides() -> None:
    """Ordenar chave é canonização; ordenar array é perder informação.

    `[3,1,2]` e `[1,2,3]` são payloads diferentes e precisam assinar diferente.
    Um lado que "ordenasse tudo" faria os dois colidirem — e um comando
    adulterado passaria pela verificação do outro.
    """
    subida = _ask_the_cloud({"itens": [1, 2, 3]})["signature"]
    descida = _ask_the_cloud({"itens": [3, 2, 1]})["signature"]

    assert subida != descida
    assert subida == sign_command(
        secret=SECRET,
        command_uuid="cmd-1",
        device_id=DEVICE,
        kind="apply_discount",
        payload={"itens": [1, 2, 3]},
        issued_at="2026-09-19T12:00:00+00:00",
    )


def test_the_cloud_only_issues_what_the_terminal_accepts() -> None:
    """Emitir um `kind` que o terminal não conhece é emitir para o lixo."""
    kinds = _ask_the_cloud({})["kinds"]

    assert set(kinds) == {kind.value for kind in CommandKind}


def test_the_pending_count_is_visible_to_the_ui(terminal) -> None:  # noqa: ANN001
    """O caixa precisa saber que há ordem do painel esperando."""
    database, _, cloud, engine, sale = terminal
    cloud.fail_report = TransportError("sem rede")
    cloud.queue.append(_discount(str(sale.id)))

    assert engine.pending_commands() == 0
    engine.command_cycle()
    assert engine.pending_commands() == 0, "aplicado sai de pendente"

    InboxRepository(database).accept(_discount(str(sale.id), "5"))
    assert engine.pending_commands() == 1
