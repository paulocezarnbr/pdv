"""Ativação a partir do modo demonstração: banco novo, demonstração arquivada.

Um terminal que vendeu em modo demonstração tem, no banco, vendas, produtos,
usuários de teste e uma cadeia de auditoria sob o tenant de demonstração — e
tudo isso ainda na fila de saída, porque nunca houve para onde enviar. Ativar
no MESMO banco mandaria essa fila para a loja real no primeiro ciclo: vendas de
teste no faturamento de um CNPJ de verdade, que só aparecem na conciliação do
mês. E apagar a demonstração contraria a regra de que nada some fisicamente.

Por isso a ativação feita pelo caixa grava num banco **à parte**
(`pdv_local.ativacao.db`). Na próxima abertura, antes de qualquer conexão, o
banco de demonstração é renomeado para `pdv_demo-AAAAMMDD-HHMMSS.db` e o novo
assume o lugar. Renomear na abertura, e não na hora da ativação, é o que torna
a troca segura: com o caixa aberto o arquivo está travado pelo próprio processo
e pelo servidor do salão.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from pdv.domain.errors import PdvError

#: Arquivos que acompanham um banco SQLite em WAL.
_COMPANIONS = ("-wal", "-shm")


class StagedActivationError(PdvError):
    """A troca do banco de demonstração pelo da loja não pôde ser feita."""


def staged_path(database_path: Path) -> Path:
    """Onde a ativação feita pelo caixa grava o banco da loja."""
    return database_path.with_name(f"{database_path.stem}.ativacao{database_path.suffix}")


def archive_path(database_path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return database_path.with_name(f"pdv_demo-{stamp}{database_path.suffix}")


def promote_staged_activation(
    database_path: Path,
    *,
    now: datetime | None = None,
    wait_seconds: float = 15.0,
) -> Path | None:
    """Troca o banco de demonstração pelo da loja, se houver ativação pendente.

    Devolve o caminho do arquivo de demonstração, ou `None` se não havia nada a
    trocar. Espera até `wait_seconds` pelo processo anterior soltar o arquivo:
    o caixa reinicia sozinho depois de ativar, e no Windows o processo antigo
    pode ainda estar fechando quando o novo começa.

    Raises:
        StagedActivationError: o arquivo continuou travado.
    """
    staged = staged_path(database_path)
    if not staged.exists():
        return None

    archive = archive_path(database_path, now)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if database_path.exists():
                # Banco primeiro: se ele está travado, nada foi movido ainda, e
                # a próxima tentativa começa do mesmo estado.
                os.replace(database_path, archive)
                for suffix in _COMPANIONS:
                    companion = Path(f"{database_path}{suffix}")
                    if companion.exists():
                        os.replace(companion, Path(f"{archive}{suffix}"))
            os.replace(staged, database_path)
            for suffix in _COMPANIONS:
                companion = Path(f"{staged}{suffix}")
                if companion.exists():
                    os.replace(companion, Path(f"{database_path}{suffix}"))
            return archive if archive.exists() else None
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise StagedActivationError(
                    "O PDV anterior ainda está usando o banco de dados. Feche "
                    "todas as janelas do PDV e abra de novo."
                ) from exc
            time.sleep(0.5)


__all__ = [
    "StagedActivationError",
    "archive_path",
    "promote_staged_activation",
    "staged_path",
]
