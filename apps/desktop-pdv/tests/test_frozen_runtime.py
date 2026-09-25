"""O PDV.exe instalado não é o PDV da suíte — duas diferenças que já derrubaram
o caixa depois do login, na primeira vez que alguém passou do banco:

1. **Sem console.** O executável é gráfico: `sys.stdout` e `sys.stderr` são
   `None`. O uvicorn, com a configuração de log padrão dele, pergunta ao
   `sys.stdout` se o terminal tem cores — e o salão (app do garçom) morria na
   subida com "Unable to configure formatter 'default'".
2. **Log append-only.** O `harden.ps1` deixa `logs\\` só com "acrescentar" para
   o grupo Usuários. O `open(..., "a")` do Python pede escrita completa, o
   Windows recusa, e o PDV ficava sem log nenhum — exatamente quando mais
   precisava de um.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig
from pdv.data.database import Database
from pdv.edge.worker import EdgeServer
from pdv.logfile import AppendOnlyFileHandler, open_append_only


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def database(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="t",
        store_id="s",
        device_id="d",
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
    )
    db = Database(config.database_path)
    db.migrate()
    yield db, config
    db.close()


def test_the_salon_server_starts_without_a_console(database, monkeypatch) -> None:  # noqa: ANN001
    db, config = database
    monkeypatch.setenv("PDV_EDGE_TLS", "0")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    server = EdgeServer(db, config, port=_free_port(), announce=False)
    try:
        assert server.start() is True
    finally:
        server.stop()


def test_a_salon_that_cannot_start_never_takes_the_counter_down(database, monkeypatch) -> None:  # noqa: ANN001
    """O docstring do `start()` já prometia isto; a exceção escapava mesmo assim."""
    import uvicorn

    db, config = database
    monkeypatch.setenv("PDV_EDGE_TLS", "0")

    def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("Unable to configure formatter 'default'")

    monkeypatch.setattr(uvicorn, "Config", broken)
    server = EdgeServer(db, config, port=_free_port(), announce=False)
    assert server.start() is False
    assert server.is_running is False


def _restrict_to_append(path: Path) -> None:
    """A ACL que o harden.ps1 põe nos logs, aplicada ao usuário que roda o teste.

    Quem cria o arquivo é o dono, e o dono sempre pode reescrever a ACL: é
    assim que o teste consegue desfazer a restrição no fim.
    """
    me = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    ).stdout.strip().split(",")[-1].strip('"')
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{me}:(AD,REA,RA,S,RC)"],
        capture_output=True,
        check=True,
    )


def _restore(path: Path) -> None:
    subprocess.run(["icacls", str(path), "/reset"], capture_output=True, check=False)


@pytest.mark.skipif(os.name != "nt", reason="ACL do NTFS")
def test_the_counter_still_logs_into_an_append_only_file(tmp_path: Path) -> None:
    log = tmp_path / "pdv.log"
    log.write_text("linha antiga\n", encoding="utf-8")
    _restrict_to_append(log)
    try:
        with pytest.raises(PermissionError):
            open(log, "a", encoding="utf-8")  # noqa: SIM115 - o que o logging fazia

        with open_append_only(log) as stream:
            stream.write("linha nova\n")
    finally:
        _restore(log)

    assert log.read_text(encoding="utf-8") == "linha antiga\nlinha nova\n"


@pytest.mark.skipif(os.name != "nt", reason="ACL do NTFS")
def test_append_only_cannot_erase_what_is_already_there(tmp_path: Path) -> None:
    """A razão de ser do append-only: o operador não apaga o próprio rastro."""
    log = tmp_path / "pdv.log"
    log.write_text("rastro\n", encoding="utf-8")
    _restrict_to_append(log)
    try:
        with pytest.raises(PermissionError):
            open(log, "w", encoding="utf-8")  # noqa: SIM115
        with open_append_only(log) as stream:
            stream.seek(0)
            stream.write("x\n")
    finally:
        _restore(log)

    assert log.read_text(encoding="utf-8").startswith("rastro\n")


def test_the_log_handler_writes_through_append_only(tmp_path: Path) -> None:
    log = tmp_path / "pdv.log"
    handler = AppendOnlyFileHandler(log)
    record = logging.LogRecord("pdv", logging.INFO, __file__, 1, "venda %s", ("42",), None)
    handler.emit(record)
    handler.close()
    assert "venda 42" in log.read_text(encoding="utf-8")


def test_no_writable_log_folder_is_not_a_reason_to_stop(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Sem pasta gravável nenhuma, o PDV abre mesmo assim — e sem console."""
    import main

    monkeypatch.setattr(sys, "stderr", None)
    blocker = tmp_path / "um-arquivo"
    blocker.write_text("", encoding="utf-8")  # pasta impossível: o "pai" é arquivo
    monkeypatch.setattr(main, "_log_candidates", lambda config: [blocker / "pdv.log"])
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: kwargs["handlers"])
    config = AppConfig(tenant_id="t", store_id="s", device_id="d", database_path=tmp_path / "pdv.db")

    handlers = main._configure_logging(config)
    assert handlers and all(isinstance(h, logging.NullHandler) for h in handlers)
