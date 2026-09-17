"""Testes do provisionamento — detecção, cofre de segredos e configurações.

O que estes testes protegem é o momento em que o técnico vai embora da loja. A
detecção é a única parte do sistema cuja falha é **silenciosa**: um falso
positivo não levanta exceção, ele grava uma configuração plausível e o erro só
aparece no primeiro cupom, na frente do cliente.

O caso real que motivou metade deste arquivo: numa máquina de desenvolvimento a
detecção classificou uma `EPSON L8180 Series` — multifuncional a jato de tinta —
como impressora térmica, porque a pista era a marca "epson". Em produção o PDV
teria mandado ESC/POS cru para um jato de tinta e cuspido páginas de lixo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.data.repositories import OutboxRepository
from pdv.data.settings import SettingsStore
from pdv.domain.errors import ScaleNotConnectedError, ScaleTimeoutError
from pdv.domain.models import EntityId, Grams, ScaleReading, ScaleStatus
from pdv.provisioning.detection import (
    REQUIRED_VALID_READS,
    DetectedPrinter,
    DetectedScale,
    DetectionResult,
    SerialPortInfo,
    _is_thermal,
    detect_printers,
    detect_scales,
    probe_port,
    settings_from_detection,
)
from pdv.provisioning.activation import (
    ActivationBlocked,
    ActivationError,
    ActivationRefused,
    ActivationResult,
    _parse_activation,
    activate,
    is_activated,
    load_sync_token,
    normalize_code,
)
from pdv.provisioning.secrets import SECRET_LENGTH_BYTES, SecretVault


# --------------------------------------------------------------------------- #
# Dublês
# --------------------------------------------------------------------------- #


class FakeDriver:
    """Driver de balança programável. Registra o que foi tentado na porta."""

    def __init__(
        self,
        *,
        responds: bool = True,
        opens: bool = True,
        reads_before_failing: int | None = None,
        log: list[tuple[str, str, int]] | None = None,
        port: str = "",
        protocol: str = "",
        baudrate: int = 0,
    ) -> None:
        self._responds = responds
        self._opens = opens
        self._budget = reads_before_failing
        self._log = log
        self._port = port
        self._protocol = protocol
        self._baudrate = baudrate
        self.is_open = False
        self.closed = False

    def open(self) -> None:
        if self._log is not None:
            self._log.append((self._port, self._protocol, self._baudrate))
        if not self._opens:
            raise ScaleNotConnectedError(f"porta {self._port} ocupada")
        self.is_open = True

    def close(self) -> None:
        self.closed = True
        self.is_open = False

    def read(self) -> ScaleReading:
        if not self._responds:
            raise ScaleTimeoutError("sem resposta")
        if self._budget is not None:
            if self._budget <= 0:
                raise ScaleTimeoutError("acabou o crédito de leituras")
            self._budget -= 1
        return ScaleReading(
            status=ScaleStatus.STABLE,
            weight_grams=Grams(0),
            raw_frame="\x0200000\x03",
        )


def factory_for(spec: dict[tuple[str, str], bool], log: list | None = None):
    """Fábrica que responde apenas nas combinações `(porta, protocolo)` dadas."""

    def _factory(port: str, protocol_name: str, baudrate: int) -> FakeDriver:
        return FakeDriver(
            responds=spec.get((port, protocol_name), False),
            log=log,
            port=port,
            protocol=protocol_name,
            baudrate=baudrate,
        )

    return _factory


# --------------------------------------------------------------------------- #
# Classificação de impressora — onde o falso positivo mora
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "EPSON TM-T20X Receipt",
        "EPSON TM-T88VI",
        "Bematech MP-4200 TH",
        "Elgin i9",
        "DARUMA DR800",
        "POS-80 Printer",
        "Impressora Termica 80mm",
    ],
)
def test_thermal_models_are_recognised(name: str) -> None:
    assert _is_thermal(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "EPSON0B51F3 (L8180 Series)",  # o falso positivo real
        "HP LaserJet Pro M404",
        "EPSON L3250 Series",
        "Microsoft Print to PDF",
        "OneNote (Desktop)",
        "Fax",
        "Microsoft XPS Document Writer",
        "Adobe PDF",
    ],
)
def test_office_and_virtual_printers_are_rejected(name: str) -> None:
    """A marca não decide. "epson" casaria com jato de tinta e com projetor."""
    assert _is_thermal(name) is False


def test_default_printer_is_never_promoted_by_being_default() -> None:
    """A padrão do Windows costuma ser jato de tinta ou "Print to PDF".

    Promover a padrão por falta de candidata melhor é exatamente como o cupom
    vira página A4 de lixo. Sem térmica reconhecida, a resposta certa é `None`.
    """
    result = DetectionResult(
        printers=(
            DetectedPrinter("Microsoft Print to PDF", is_thermal_candidate=False),
            DetectedPrinter(
                "EPSON0B51F3 (L8180 Series)",
                is_thermal_candidate=False,
                is_default=True,
            ),
        )
    )

    assert result.best_printer is None


def test_thermal_wins_over_default() -> None:
    result = DetectionResult(
        printers=(
            DetectedPrinter("Microsoft Print to PDF", False, is_default=True),
            DetectedPrinter("EPSON TM-T20X Receipt", True),
        )
    )

    assert result.best_printer is not None
    assert result.best_printer.name == "EPSON TM-T20X Receipt"


def test_detect_printers_uses_injected_enumerator() -> None:
    printers = detect_printers(
        lambda: [("EPSON TM-T20X Receipt", False), ("Microsoft Print to PDF", True)]
    )

    assert [p.is_thermal_candidate for p in printers] == [True, False]
    assert [p.is_default for p in printers] == [False, True]


# --------------------------------------------------------------------------- #
# Sondagem de porta
# --------------------------------------------------------------------------- #


def test_probe_identifies_responding_protocol() -> None:
    port = SerialPortInfo("COM3", "USB-SERIAL CH340")
    detected = probe_port(
        port,
        baudrates=(9600,),
        driver_factory=factory_for({("COM3", "filizola"): True}),
    )

    assert detected is not None
    assert detected.protocol == "filizola"
    assert detected.port == "COM3"
    assert detected.valid_reads >= REQUIRED_VALID_READS


def test_probe_returns_none_when_nothing_answers() -> None:
    detected = probe_port(
        SerialPortInfo("COM9"),
        baudrates=(9600,),
        driver_factory=factory_for({}),
    )

    assert detected is None


def test_probe_listens_before_it_writes() -> None:
    """Protocolo de streaming vem antes do de requisição.

    Mandar `ENQ` para um equipamento que não é balança pode fazer uma impressora
    serial cuspir papel ou travar um leitor. A escuta passiva é inofensiva, então
    é ela que tenta primeiro.
    """
    log: list[tuple[str, str, int]] = []
    probe_port(
        SerialPortInfo("COM3"),
        baudrates=(9600,),
        driver_factory=factory_for({}, log=log),
    )

    protocols_tried = [protocol for _, protocol, _ in log]
    assert protocols_tried[0] == "filizola"
    assert protocols_tried.index("filizola") < protocols_tried.index("toledo_prix3")


def test_probe_requires_several_consistent_reads() -> None:
    """Uma leitura isolada pode ser coincidência de bytes; três, não."""
    driver = FakeDriver(reads_before_failing=REQUIRED_VALID_READS - 1)

    detected = probe_port(
        SerialPortInfo("COM3"),
        baudrates=(9600,),
        driver_factory=lambda *_a, **_k: driver,
    )

    assert detected is None


def test_probe_closes_the_port_even_when_reads_fail() -> None:
    """Porta deixada aberta impede a próxima tentativa e o próprio PDV depois."""
    drivers: list[FakeDriver] = []

    def factory(port: str, protocol_name: str, baudrate: int) -> FakeDriver:
        driver = FakeDriver(responds=False, port=port)
        drivers.append(driver)
        return driver

    probe_port(SerialPortInfo("COM3"), baudrates=(9600,), driver_factory=factory)

    assert drivers
    assert all(d.closed for d in drivers)


def test_busy_port_is_not_an_error() -> None:
    """Porta ocupada é rotina numa máquina com vários periféricos."""
    detected = probe_port(
        SerialPortInfo("COM3"),
        baudrates=(9600,),
        driver_factory=lambda *_a, **_k: FakeDriver(opens=False),
    )

    assert detected is None


def test_adapter_ports_are_probed_first() -> None:
    """A balança quase sempre está atrás de um CH340/FTDI.

    Tentar essas portas antes encurta uma varredura que, no pior caso, leva
    dezenas de segundos com o técnico esperando na frente do balcão.
    """
    log: list[tuple[str, str, int]] = []
    ports = [
        SerialPortInfo("COM1", "Porta de comunicação"),
        SerialPortInfo("COM7", "USB-SERIAL CH340"),
    ]

    detect_scales(ports, driver_factory=factory_for({}, log=log))

    assert log[0][0] == "COM7"


def test_detect_scales_finds_one_per_port() -> None:
    ports = [SerialPortInfo("COM1"), SerialPortInfo("COM7")]
    found = detect_scales(
        ports,
        driver_factory=factory_for(
            {("COM1", "toledo_prix3"): True, ("COM7", "filizola"): True}
        ),
    )

    assert {(s.port, s.protocol) for s in found} == {
        ("COM1", "toledo_prix3"),
        ("COM7", "filizola"),
    }


# --------------------------------------------------------------------------- #
# Tradução para configurações
# --------------------------------------------------------------------------- #


def test_settings_fall_back_to_harmless_backends() -> None:
    """Sem hardware, o PDV abre e avisa — não trava lendo de uma COM inventada."""
    values = settings_from_detection(DetectionResult())

    assert values["scale.protocol"] == "simulated"
    assert values["printer.backend"] == "file"
    assert "scale.port" not in values
    assert "printer.name" not in values


def test_settings_from_full_detection() -> None:
    result = DetectionResult(
        scales=(
            __import__("pdv.provisioning.detection", fromlist=["DetectedScale"])
            .DetectedScale("COM7", "filizola", 9600, 4, "\x0200892\x03"),
        ),
        printers=(DetectedPrinter("EPSON TM-T20X Receipt", True),),
    )

    values = settings_from_detection(result)

    assert values == {
        "scale.protocol": "filizola",
        "scale.port": "COM7",
        "scale.baudrate": "9600",
        "printer.backend": "win32raw",
        "printer.name": "EPSON TM-T20X Receipt",
    }


# --------------------------------------------------------------------------- #
# Cofre de segredos
# --------------------------------------------------------------------------- #


def test_device_secret_is_generated_once_and_reused(tmp_path: Path) -> None:
    """Regerar o segredo invalidaria toda a cadeia de auditoria já gravada."""
    vault = SecretVault(tmp_path / "secrets")

    first = vault.ensure_device_secret()
    second = vault.ensure_device_secret()

    assert first == second
    assert len(first) == SECRET_LENGTH_BYTES


def test_secret_is_not_stored_in_plain_text(tmp_path: Path) -> None:
    """Fora do Windows o fallback é Base64 — sem proteção, mas nunca literal."""
    vault = SecretVault(tmp_path / "secrets")
    value = b"segredo-do-terminal-xyz"

    vault.store("device_secret", value)
    on_disk = (tmp_path / "secrets" / "device_secret.bin").read_bytes()

    assert value not in on_disk
    assert vault.load("device_secret") == value


def test_missing_secret_returns_none(tmp_path: Path) -> None:
    assert SecretVault(tmp_path / "secrets").load("nao_existe") is None
    assert SecretVault(tmp_path / "secrets").exists("nao_existe") is False


def test_store_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """A escrita é atômica: o `.tmp` tem que sumir, senão sobra lixo cifrado."""
    directory = tmp_path / "secrets"
    vault = SecretVault(directory)
    vault.store("device_secret", b"abc")

    assert not list(directory.glob("*.tmp"))


# --------------------------------------------------------------------------- #
# SettingsStore
# --------------------------------------------------------------------------- #


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "pdv.db")
    db.migrate()
    return db


def test_set_many_is_atomic_on_failure(database: Database) -> None:
    """Meia configuração é pior que nenhuma: o PDV abriria com porta sem protocolo."""
    store = SettingsStore(database)

    with pytest.raises(Exception):
        store.set_many({"scale.port": "COM7", "scale.baudrate": None})  # type: ignore[dict-item]

    assert store.get("scale.port") is None


def test_settings_override_config(database: Database) -> None:
    """O que o instalador descobriu no hardware real vence o padrão do código."""
    store = SettingsStore(database)
    store.set_many(
        {
            "scale.protocol": "filizola",
            "scale.port": "COM7",
            "scale.baudrate": "19200",
            "printer.backend": "win32raw",
            "printer.name": "EPSON TM-T20X Receipt",
        }
    )

    config = store.apply_to(
        AppConfig(
            tenant_id="t",
            store_id="s",
            device_id="d",
            scale=ScaleConfig(protocol="simulated", port="COM1"),
            printer=PrinterConfig(backend="file"),
        )
    )

    assert config.scale.protocol == "filizola"
    assert config.scale.port == "COM7"
    assert config.scale.baudrate == 19200
    assert config.printer.backend == "win32raw"
    assert config.printer.windows_printer_name == "EPSON TM-T20X Receipt"


def test_partial_settings_keep_config_defaults(database: Database) -> None:
    """Porta gravada sem protocolo não pode sobrescrever nada pela metade."""
    store = SettingsStore(database)
    store.set("scale.port", "COM7")

    config = store.apply_to(
        AppConfig(
            tenant_id="t",
            store_id="s",
            device_id="d",
            scale=ScaleConfig(protocol="simulated", port="COM1"),
        )
    )

    assert config.scale.protocol == "simulated"
    assert config.scale.port == "COM1"


def test_settings_preserve_device_secret(database: Database) -> None:
    """O segredo vem do DPAPI, nunca do banco que ele protege."""
    store = SettingsStore(database)
    store.set("device.tenant_id", "novo-tenant")

    config = store.apply_to(
        AppConfig(
            tenant_id="t", store_id="s", device_id="d", device_secret=b"chave-real"
        )
    )

    assert config.tenant_id == "novo-tenant"
    assert config.device_secret == b"chave-real"


# --------------------------------------------------------------------------- #
# Ativação do terminal
# --------------------------------------------------------------------------- #


class FakeActivationTransport:
    """Retaguarda falsa. Guarda o que recebeu e devolve o que for programado."""

    def __init__(self, result: ActivationResult | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.codes_received: list[str] = []
        self.fingerprints: list[dict] = []

    def activate(self, code: str, fingerprint: dict) -> ActivationResult:
        self.codes_received.append(code)
        self.fingerprints.append(fingerprint)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _queue_unsynced_sale(database: Database) -> None:
    """Deixa uma venda na fila, pelo mesmo caminho que o caixa usa."""
    with database.transaction() as connection:
        OutboxRepository().enqueue(
            connection,
            entity_table="orders",
            entity_id=EntityId("pedido-1"),
            client_uuid=EntityId("uuid-venda"),
            operation="insert",
            payload={"total_cents": 1000},
        )


def _result(tenant: str = "tenant-a") -> ActivationResult:
    return ActivationResult(
        tenant_id=tenant,
        store_id="loja-1",
        device_id="device-1",
        sync_token="token-secreto-123",
        store_name="Confeitaria da Esquina",
        cloud_base_url="https://api.exemplo",
    )


@pytest.mark.parametrize(
    ("digitado", "esperado"),
    [
        ("a1b2-c3d4", "A1B2C3D4"),
        ("  A1B2 C3D4  ", "A1B2C3D4"),
        ("a1b2.c3d4", "A1B2C3D4"),
    ],
)
def test_code_is_normalised(digitado: str, esperado: str) -> None:
    """O código é ditado por telefone e digitado no balcão, não colado."""
    assert normalize_code(digitado) == esperado


@pytest.mark.parametrize("digitado", ["", "abc", "---", "x" * 40])
def test_implausible_codes_are_rejected_before_the_network(digitado: str) -> None:
    with pytest.raises(ActivationError):
        normalize_code(digitado)


def test_activation_persists_identity_and_token(
    database: Database, tmp_path: Path
) -> None:
    vault = SecretVault(tmp_path / "secrets")
    transport = FakeActivationTransport(_result())

    result = activate(
        "a1b2-c3d4", database=database, vault=vault, transport=transport
    )

    settings = SettingsStore(database).load()
    assert settings.activated is True
    assert settings.tenant_id == "tenant-a"
    assert settings.device_id == "device-1"
    assert settings.cloud_base_url == "https://api.exemplo"
    assert result.store_name == "Confeitaria da Esquina"
    # O código chega normalizado ao servidor, não como foi digitado.
    assert transport.codes_received == ["A1B2C3D4"]


def test_token_goes_to_the_vault_never_to_the_database(
    database: Database, tmp_path: Path
) -> None:
    """Guardar o token no banco que o operador pode abrir o entregaria de graça."""
    vault = SecretVault(tmp_path / "secrets")
    activate("a1b2-c3d4", database=database, vault=vault, transport=FakeActivationTransport(_result()))

    assert load_sync_token(vault) == "token-secreto-123"

    stored = {
        row["key"]: row["value"]
        for row in database.query_all("SELECT key, value FROM device_settings")
    }
    assert "token-secreto-123" not in stored.values()
    assert not any("token" in key for key in stored)


def test_retenanting_with_pending_sales_is_blocked(
    database: Database, tmp_path: Path
) -> None:
    """O erro de campo que ninguém percebe no dia.

    Vendas na fila foram registradas sob o CNPJ antigo. Reapontar o terminal
    antes de esvaziá-la manda o faturamento de uma loja para a outra — e isso
    só aparece na conciliação fiscal do mês.
    """
    vault = SecretVault(tmp_path / "secrets")
    activate("a1b2-c3d4", database=database, vault=vault, transport=FakeActivationTransport(_result("tenant-a")))

    _queue_unsynced_sale(database)

    with pytest.raises(ActivationBlocked) as exc:
        activate(
            "e5f6-a7b8",
            database=database,
            vault=vault,
            transport=FakeActivationTransport(_result("tenant-b")),
        )

    assert "sincronizad" in str(exc.value)
    # A identidade antiga fica intacta: nada foi gravado pela metade.
    assert SettingsStore(database).load().tenant_id == "tenant-a"


def test_retenanting_is_allowed_once_the_queue_is_empty(
    database: Database, tmp_path: Path
) -> None:
    vault = SecretVault(tmp_path / "secrets")
    activate("a1b2-c3d4", database=database, vault=vault, transport=FakeActivationTransport(_result("tenant-a")))

    activate(
        "e5f6-a7b8",
        database=database,
        vault=vault,
        transport=FakeActivationTransport(_result("tenant-b")),
    )

    assert SettingsStore(database).load().tenant_id == "tenant-b"


def test_reactivating_the_same_tenant_is_never_blocked(
    database: Database, tmp_path: Path
) -> None:
    """Renovar credencial da própria loja é rotina de suporte, não troca."""
    vault = SecretVault(tmp_path / "secrets")
    activate("a1b2-c3d4", database=database, vault=vault, transport=FakeActivationTransport(_result("tenant-a")))

    _queue_unsynced_sale(database)

    activate(
        "e5f6-a7b8",
        database=database,
        vault=vault,
        transport=FakeActivationTransport(_result("tenant-a")),
    )

    assert SettingsStore(database).load().activated is True


def test_refusal_leaves_the_terminal_untouched(
    database: Database, tmp_path: Path
) -> None:
    """Código expirado não pode deixar o terminal meio ativado."""
    vault = SecretVault(tmp_path / "secrets")
    transport = FakeActivationTransport(error=ActivationRefused("código expirado"))

    with pytest.raises(ActivationRefused):
        activate("a1b2-c3d4", database=database, vault=vault, transport=transport)

    assert is_activated(database) is False
    assert load_sync_token(vault) is None


def test_incomplete_server_response_is_rejected() -> None:
    """Resposta sem token deixaria o terminal "ativado" sem poder sincronizar."""
    with pytest.raises(ActivationError) as exc:
        _parse_activation(
            {"tenant_id": "t", "store_id": "s", "device_id": "d"}, "https://api"
        )

    assert "sync_token" in str(exc.value)


def test_fingerprint_is_sent_for_the_panel_to_identify_the_terminal(
    database: Database, tmp_path: Path
) -> None:
    transport = FakeActivationTransport(_result())
    activate(
        "a1b2-c3d4",
        database=database,
        vault=SecretVault(tmp_path / "secrets"),
        transport=transport,
    )

    assert set(transport.fingerprints[0]) == {"hostname", "os", "arch"}


# --------------------------------------------------------------------------- #
# Atualização in-place
# --------------------------------------------------------------------------- #


def test_update_never_downgrades_a_working_scale(database: Database) -> None:
    """A regressão que uma atualização de rotina poderia causar.

    Balança desligada no instante do update. Se a detecção sobrescrevesse, o
    terminal cairia para `simulated` e a loja pararia de vender produto por peso
    sem que nada tivesse de fato quebrado — e ninguém ligaria uma coisa à outra.
    """
    store = SettingsStore(database)
    store.set_many({"scale.protocol": "filizola", "scale.port": "COM7"})

    # O que a detecção devolveria com a balança desligada:
    applied = store.set_many_if_absent(settings_from_detection(DetectionResult()))

    settings = store.load()
    assert settings.scale_protocol == "filizola"
    assert settings.scale_port == "COM7"
    assert "scale.protocol" not in applied


def test_update_fills_only_what_is_missing(database: Database) -> None:
    store = SettingsStore(database)
    store.set("printer.name", "EPSON TM-T20X Receipt")
    store.set("printer.backend", "win32raw")

    applied = store.set_many_if_absent(
        {
            "printer.backend": "file",
            "printer.name": "outra",
            "scale.protocol": "simulated",
        }
    )

    assert applied == {"scale.protocol": "simulated"}
    assert store.load().printer_name == "EPSON TM-T20X Receipt"


def test_explicit_redetection_does_overwrite(database: Database) -> None:
    """Trocou a balança? O atalho "Reconfigurar periféricos" tem que valer."""
    store = SettingsStore(database)
    store.set_many({"scale.protocol": "filizola", "scale.port": "COM7"})

    store.set_many(
        settings_from_detection(
            DetectionResult(
                scales=(DetectedScale("COM3", "toledo_prix3", 9600, 5, ""),)
            )
        )
    )

    settings = store.load()
    assert settings.scale_protocol == "toledo_prix3"
    assert settings.scale_port == "COM3"


def test_empty_stored_value_is_treated_as_absent(database: Database) -> None:
    """Chave gravada vazia é configuração ausente, não configuração válida."""
    store = SettingsStore(database)
    store.set("printer.name", "")

    applied = store.set_many_if_absent({"printer.name": "EPSON TM-T20X Receipt"})

    assert applied == {"printer.name": "EPSON TM-T20X Receipt"}
