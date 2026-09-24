"""De onde o PDV instalado tira quem ele é.

O defeito que este módulo fecha
-------------------------------

O instalador roda o `PDVSetup.exe`, que grava tudo em
`C:\\ProgramData\\ERPFood\\PDV`: o banco, a ativação (tenant, loja, terminal,
endereço da nuvem), a impressora e a balança detectadas, e o segredo do ledger
no cofre DPAPI. O `PDV.exe`, porém, montava a configuração **só do ambiente**:
abria `./pdv_local.db` (relativo à pasta de trabalho — Program Files, onde o
endurecimento deixa o usuário só com leitura), com os IDs de demonstração e a
chave de desenvolvimento. Nada do que o provisionamento gravou chegava ao caixa:
a venda saía assinada com a chave errada, sob o tenant errado, e a nuvem a
recusava. E o catálogo de demonstração era semeado a cada abertura — os "itens
fantasma" que o provisionamento tem o cuidado de não criar.

Agora os dois executáveis leem o mesmo lugar, pelo mesmo caminho.

Rodando do código-fonte
-----------------------

`python main.py` continua como sempre foi: banco em `./pdv_local.db`, chave de
desenvolvimento, catálogo de demonstração. Só vira "instalado" quando é o
executável congelado, ou quando `PDV_DATA_DIR` é definido — que é como se
testa o caminho de produção sem compilar.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.settings import SettingsStore
from pdv.provisioning.secrets import SecretVault

#: Onde o instalador põe os dados (ver `packaging/installer.iss`, `DataDir`).
DEFAULT_DATA_DIR = Path(r"C:\ProgramData\ERPFood\PDV")
DATABASE_NAME = "pdv_local.db"


@dataclass(frozen=True, slots=True)
class Runtime:
    config: AppConfig
    database: Database
    vault: SecretVault
    #: Instalado (executável ou `PDV_DATA_DIR`) — ou rodando do código-fonte.
    installed: bool


def data_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("PDV_DATA_DIR") or DEFAULT_DATA_DIR)


def is_installed(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(getattr(sys, "frozen", False) or env.get("PDV_DATA_DIR"))


def load_runtime(environ: dict[str, str] | None = None) -> Runtime:
    """Abre o banco e devolve a configuração efetiva do terminal.

    Instalado, a ordem é: o ambiente dá os padrões; a pasta de dados dá o banco
    e o cofre; o cofre dá o segredo do ledger; o banco dá o que a ativação e a
    detecção gravaram. Cada camada só sobrescreve o que conhece.
    """
    env = os.environ if environ is None else environ
    base = AppConfig.from_env()

    if not is_installed(env):
        database = Database(base.database_path)
        database.migrate()
        vault = SecretVault(base.database_path.parent / "secrets")
        return Runtime(base, database, vault, installed=False)

    root = data_dir(env)
    root.mkdir(parents=True, exist_ok=True)
    database = Database(root / DATABASE_NAME)
    database.migrate()
    vault = SecretVault(root / "secrets")

    # `PDV_DEVICE_SECRET` só vale para teste: em campo o segredo é do cofre, e o
    # mesmo que assinou o ledger desde o primeiro evento.
    secret = (
        env["PDV_DEVICE_SECRET"].encode("utf-8")
        if env.get("PDV_DEVICE_SECRET")
        else vault.ensure_device_secret()
    )
    config = SettingsStore(database).apply_to(
        replace(
            base,
            database_path=root / DATABASE_NAME,
            device_secret=secret,
            # Cupom de contingência na pasta de dados: Program Files é só
            # leitura para o usuário da loja (ver `setup_wizard.py`).
            printer=replace(base.printer, output_dir=root / "cupons"),
        )
    )
    return Runtime(config, database, vault, installed=True)


def should_seed_demo(runtime: Runtime, environ: dict[str, str] | None = None) -> bool:
    """Catálogo de demonstração: sempre no código-fonte, nunca na loja.

    Instalado, só com `PDV_DEMO=1`. Na loja real o cardápio desce da nuvem; o
    "Bolo de Chocolate" de demonstração apareceria na busca do operador no meio
    de uma venda de verdade.
    """
    env = os.environ if environ is None else environ
    return not runtime.installed or env.get("PDV_DEMO") == "1"


__all__ = [
    "DATABASE_NAME",
    "DEFAULT_DATA_DIR",
    "Runtime",
    "data_dir",
    "is_installed",
    "load_runtime",
    "should_seed_demo",
]
