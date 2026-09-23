"""Autoteste do pacote — roda **dentro** do executável compilado.

O problema que isto resolve
---------------------------

A suíte de testes roda contra o código-fonte, onde todo arquivo está no lugar e
todo módulo é importável. O executável empacotado é outro programa: o
PyInstaller monta a árvore de imports por análise estática, e **todo import
tardio é invisível para ela**.

Este projeto está cheio de imports tardios, e cada um por um bom motivo:

* `argon2` e `cryptography` entram dentro de funções para que a ausência delas
  degrade o sistema em vez de impedir o caixa de abrir;
* o `uvicorn` resolve loop, protocolo e ciclo de vida por string, em tempo de
  execução;
* `schema.sql` e o app do garçom são lidos por caminho relativo ao módulo.

O resultado é que um `hiddenimports` incompleto produz um pacote que **instala
e abre**, e falha depois: o gerente descobre que não consegue autorizar um
cancelamento na frente do cliente, ou o garçom abre o app e recebe um 500. A
suíte verde não diz nada sobre isso, porque ela nunca rodou dentro do pacote.

`PDV.exe --selftest` roda aqui, no binário entregue, e confere justamente o que
só quebra ali. É a última etapa do `build.ps1`, antes de assinar.

O que ele **não** é
-------------------

Não substitui a suíte. Não testa regra de negócio nenhuma — isso já está
coberto e rodar de novo dentro do pacote não acrescentaria informação. Ele
responde uma pergunta só: *este pacote está completo?*
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

#: Cada item é (nome legível, o que acontece se faltar, a verificação).
#: A consequência entra no relatório porque, quando isto falha às 23h de uma
#: sexta antes de publicar, saber o que quebra na loja decide se a entrega sai.
Check = tuple[str, str, Callable[[], str]]


def _schema() -> str:
    from pdv.data.database import _SCHEMA_FILE

    content = _SCHEMA_FILE.read_text(encoding="utf-8")
    if "CREATE TABLE IF NOT EXISTS orders" not in content:
        raise RuntimeError("schema.sql veio truncado")
    return f"{len(content)} bytes"


def _migrations() -> str:
    from pdv.data.database import SCHEMA_VERSION, Database

    with tempfile.TemporaryDirectory() as folder:
        database = Database(Path(folder) / "selftest.db")
        try:
            database.migrate()
            version = int(
                database.connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            database.close()

    if version != SCHEMA_VERSION:
        raise RuntimeError(f"migrou para {version}, esperava {SCHEMA_VERSION}")
    return f"banco novo na versão {version}"


def _webapp() -> str:
    from pdv.edge.webapp import WEBAPP_DIR, index_html

    html = index_html()
    if "renderStaffLogin" not in html:
        raise RuntimeError("index.html veio de uma versão antiga")

    vendor = WEBAPP_DIR / "vendor" / "sweetalert2.min.js"
    if not vendor.is_file():
        raise RuntimeError("o SweetAlert2 não entrou no pacote")
    return f"{len(html) // 1024} KiB + vendor"


def _argon2() -> str:
    from pdv.services.authorization import _verify, hash_pin

    digest = hash_pin("481592")
    if not _verify(digest, "481592") or _verify(digest, "481593"):
        raise RuntimeError("o Argon2id não está validando corretamente")
    return "hash e verificação"


def _tls() -> str:
    from pdv.edge.tls import ensure_certificate

    with tempfile.TemporaryDirectory() as folder:
        material = ensure_certificate(
            Path(folder), store_name="Autoteste", hosts=("localhost", "127.0.0.1")
        )
    if material is None:
        # `ensure_certificate` engole a falha de propósito, para que o salão
        # caia para HTTP em vez de derrubar o caixa. Aqui, no autoteste, esse
        # silêncio é exatamente o que precisamos quebrar.
        raise RuntimeError(
            "não gerou certificado — o salão subiria em HTTP, com o PIN "
            "e o token do aparelho em claro na rede da loja"
        )
    return material.short_fingerprint


def _uvicorn() -> str:
    """Os módulos que o uvicorn resolve por string, um a um.

    Importá-los aqui é o teste: se algum não entrou no pacote, o servidor do
    salão morreria ao subir — e o log diria apenas "servidor indisponível".
    """
    import importlib

    names = (
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
    )
    for name in names:
        importlib.import_module(name)
    return f"{len(names)} módulos resolvidos por string"


def _edge_app() -> str:
    """Monta a aplicação FastAPI de verdade e confere as rotas que importam.

    Montar é o que pega o erro real: um `from __future__ import annotations`
    perdido em `server.py` já fez **toda** rota responder 422 com os serviços
    100% verdes por baixo.
    """
    from pdv.config import AppConfig
    from pdv.data.database import Database
    from pdv.edge.server import create_app

    with tempfile.TemporaryDirectory() as folder:
        config = AppConfig(
            tenant_id="00000000-0000-0000-0000-000000000000",
            store_id="00000000-0000-0000-0000-000000000000",
            device_id="00000000-0000-0000-0000-000000000000",
            database_path=Path(folder) / "selftest.db",
        )
        database = Database(config.database_path)
        try:
            database.migrate()
            app = create_app(database, config)
        finally:
            database.close()

    paths = {route.path for route in app.routes}
    required = {"/", "/pair", "/staff/session", "/orders", "/tables", "/kds/stream"}
    missing = required - paths
    if missing:
        raise RuntimeError(f"rotas ausentes: {sorted(missing)}")
    return f"{len(paths)} rotas"


def _printer() -> str:
    from pdv.config import PrinterConfig
    from pdv.hardware.printer.backends import build_printer

    build_printer(PrinterConfig(backend="file", output_dir=Path(tempfile.gettempdir())))
    return "backend de arquivo"


def _serial() -> str:
    import serial.tools.list_ports

    return f"{len(list(serial.tools.list_ports.comports()))} porta(s) COM"


def _qt() -> str:
    from PySide6.QtWidgets import QApplication  # noqa: F401

    from pdv.ui.counter_window import CounterWindow  # noqa: F401
    from pdv.ui.login_dialog import LoginDialog  # noqa: F401
    from pdv.ui.tables_dialog import TablesDialog  # noqa: F401

    return "janelas importáveis"


def _fiscal_engine() -> str:
    """O PyNFe e o que ele abre por caminho em tempo de execução.

    O motor fiscal só é importado quando o tenant habilita NFC-e. Sem esta
    verificação, um pacote sem a pasta `data/` do PyNFe funcionaria no caixa
    por meses e quebraria no dia da primeira emissão: a tabela de municípios do
    IBGE é lida por caminho relativo ao `__file__` da biblioteca, e o analisador
    do PyInstaller não enxerga isso.
    """
    import importlib.metadata

    from pynfe.utils import carregar_arquivo_municipios

    rio = carregar_arquivo_municipios(33)  # RJ
    if rio.get("3304557") is None:  # código IBGE do município do Rio de Janeiro
        raise RuntimeError("a tabela de municípios do PyNFe veio incompleta")

    # A LGPL do PyNFe exige que os avisos acompanhem o binário redistribuído.
    # `copy_metadata` no spec é o que os traz; sem ele, isto levanta.
    version = importlib.metadata.version("PyNFe")
    return f"PyNFe {version}, {len(rio)} municípios do RJ, licença presente"


def _danfe() -> str:
    """Monta um DANFE NFC-e completo e confere o que só o pacote pode quebrar.

    A codificação PC850 (acentos da impressora) e o QR Code nativo passam por
    tabelas de codec que o PyInstaller pode deixar de fora.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from pdv.config import PrinterConfig
    from pdv.domain.models import PaymentMethod
    from pdv.fiscal.danfe import (
        DanfeIssuer,
        DanfeItem,
        DanfePayment,
        NfceDanfe,
        access_key_check_digit,
        build_nfce_danfe,
    )

    base = "33" "2609" "12345678000190" "65" "001" "000000001" "9" "12345678"
    qr = "https://exemplo.invalid/qrcode?p=autoteste"
    payload = build_nfce_danfe(
        NfceDanfe(
            issuer=DanfeIssuer("Autoteste Ltda", "12345678000190", "1", "Rua A, 1"),
            environment="homologation",
            emission="offline_contingency",
            series=1,
            number=1,
            issued_at=datetime.now(timezone.utc),
            items=(DanfeItem("X", "Café", Decimal("1"), "UN", 700, 700),),
            payments=(DanfePayment(PaymentMethod.CASH, 700),),
            access_key=base + access_key_check_digit(base),
            consultation_url="exemplo.invalid/consulta",
            qr_code=qr,
        ),
        PrinterConfig(),
    )
    if qr.encode() not in payload or "CONTINGÊNCIA".encode("cp850") not in payload:
        raise RuntimeError("o DANFE saiu sem o QR Code ou sem a acentuação")
    return f"{len(payload)} bytes, QR e PC850"


CHECKS: tuple[Check, ...] = (
    ("schema.sql", "o caixa não abre: falha na primeira migration", _schema),
    ("migrations", "o banco da loja fica numa versão inconsistente", _migrations),
    ("app do garçom", "o celular pareia e recebe 500 na primeira tela", _webapp),
    ("Argon2id", "ninguém entra no caixa, e nenhum gerente autoriza", _argon2),
    ("TLS do salão", "PIN e token em claro na rede da loja", _tls),
    ("uvicorn", "o servidor do salão morre ao subir", _uvicorn),
    ("rotas do salão", "o app do garçom não lança pedido", _edge_app),
    ("impressora", "a venda fecha e o cupom não sai", _printer),
    ("motor fiscal", "a primeira NFC-e da loja falha meses depois da instalação",
     _fiscal_engine),
    ("DANFE NFC-e", "a nota sai sem QR Code ou com acentos ilegíveis", _danfe),
    ("porta serial", "a balança não é encontrada", _serial),
    ("interface", "o PDV não abre janela nenhuma", _qt),
)


def run(stream=sys.stdout) -> int:  # noqa: ANN001
    """Roda todas as verificações. Devolve o código de saída do processo.

    Não para na primeira falha: um pacote quebrado costuma estar quebrado em
    mais de um lugar, e descobrir um problema por rodada transformaria a
    publicação numa sequência de builds de vinte minutos.
    """
    print("Autoteste do pacote do PDV", file=stream)
    print("=" * 62, file=stream)

    failures: list[tuple[str, str, str]] = []
    for name, consequence, check in CHECKS:
        try:
            detail = check()
        except Exception as exc:  # noqa: BLE001 - o relatório é o produto
            failures.append((name, consequence, f"{type(exc).__name__}: {exc}"))
            print(f"  FALHOU  {name}", file=stream)
        else:
            print(f"  ok      {name:<18} {detail}", file=stream)

    print("=" * 62, file=stream)
    if not failures:
        print("Pacote completo.", file=stream)
        return 0

    print(f"{len(failures)} verificação(ões) falharam:\n", file=stream)
    for name, consequence, error in failures:
        print(f"  {name}", file=stream)
        print(f"    erro:     {error}", file=stream)
        print(f"    na loja:  {consequence}", file=stream)
        print(file=stream)
    print("NÃO PUBLIQUE este pacote.", file=stream)
    return 1


def run_cli() -> int:
    """Roda o autoteste e garante que o relatório sobreviva à falta de console.

    O `PDV.exe` é compilado com `console=False` — um app de caixa não abre
    janela preta. O efeito colateral é que, no executável empacotado,
    `sys.stdout` pode ser `None`: o relatório se perderia inteiro, e o
    `print` ainda levantaria `AttributeError` antes disso, fazendo o autoteste
    "falhar" por um motivo que não tem nada a ver com o pacote.

    Por isso o relatório vai **sempre** para um arquivo, e para o console
    apenas quando existe um. O `build.ps1` lê o arquivo.
    """
    report = _report_path()
    with report.open("w", encoding="utf-8") as handle:
        code = run(_Tee(handle, sys.stdout))

    if sys.stdout is None:  # pragma: no cover - só no binário sem console
        # O caminho do relatório é a única coisa que sai pelo código de saída
        # sozinho não diria. Sem console não há onde escrever — mas o arquivo
        # está lá, e o build sabe onde procurar.
        pass
    return code


def _report_path() -> Path:
    """Ao lado do executável durante o build; no temp se não der para escrever.

    Instalado em Program Files o diretório é somente-leitura para o operador,
    e o autoteste não pode falhar por não conseguir gravar o próprio relatório.
    """
    beside = Path(sys.argv[0]).resolve().parent / "selftest.log"
    try:
        beside.touch()
        return beside
    except OSError:
        return Path(tempfile.gettempdir()) / "pdv-selftest.log"


class _Tee:
    """Escreve nos dois destinos, ignorando o que não existe."""

    def __init__(self, *streams) -> None:  # noqa: ANN002
        self._streams = [s for s in streams if s is not None]

    def write(self, text: str) -> int:
        for stream in self._streams:
            try:
                stream.write(text)
            except Exception:  # noqa: BLE001 - console fechado não invalida o teste
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["CHECKS", "run", "run_cli"]
