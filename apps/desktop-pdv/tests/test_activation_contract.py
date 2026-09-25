"""Contrato da ativação — o que o PDV em C# tem de aceitar, recusar e gravar.

Os dois PDVs ativam o mesmo terminal contra a mesma retaguarda e gravam no
mesmo `device_settings` e no mesmo cofre. Um endereço que o Python aceita e o
C# recusa é um técnico sem saída no balcão; um código que um normaliza e o
outro não é um código "inválido" que estava certo.

`contracts/activation.json` guarda:

* códigos digitados e a forma que vai para a nuvem (ou o erro);
* endereços digitados e a raiz `/api` usada (ou o erro), e a volta para a tela;
* os nomes que a ativação grava: o segredo no cofre, as chaves de
  `device_settings` e os arquivos da troca do banco de demonstração.

A volta (o banco e o cofre ativados pelo C#, lidos pelo Python) é feita por
`apps/pdv-net/crosscheck.py` no CI.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_activation_contract.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pdv.config import cloud_api_root
from pdv.domain.errors import PdvError
from pdv.provisioning import activation, staging

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "activation.json"

CODES = [
    "ABCD-EFGH-JKMN", "abcd efgh jkmn", "  a1b2-c3d4 ", "A1B2C", "A1B2C3",
    "A" * 32, "A" * 33, "----", "", "ÁBCD-EFGH", "ab.cd/ef_gh",
]

SERVERS = [
    "teste.rsrassessoria.com.br", "https://teste.rsrassessoria.com.br/",
    "HTTPS://Painel.Loja.com.br/api", "painel.loja.com.br/api/", "painel.loja.com.br/erp",
    "http://localhost:3000", "http://127.0.0.1:3000/", "http://painel.loja.com.br",
    "https://user:senha@painel.loja.com.br", "ftp://painel.loja.com.br", "", "   ",
    "painel loja.com.br", "https://", "https://painel.loja.com.br:8443",
]

BASES = [
    "https://painel.loja.com.br", "https://painel.loja.com.br/", "https://painel.loja.com.br/api",
    "https://painel.loja.com.br/api/",
]


def _code(raw: str) -> dict:
    try:
        return {"raw": raw, "ok": True, "code": activation.normalize_code(raw)}
    except PdvError as error:
        return {"raw": raw, "ok": False, "message": str(error)}


def _server(raw: str) -> dict:
    try:
        url = activation.normalize_server_url(raw)
        return {"raw": raw, "ok": True, "api_url": url, "display": activation.display_server_url(url)}
    except PdvError as error:
        return {"raw": raw, "ok": False, "message": str(error)}


def _build() -> dict:
    database = Path("C:/ProgramData/ERPFood/PDV/pdv_local.db")
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_activation_contract.py. Não edite à mão.",
        "codes": [_code(raw) for raw in CODES],
        "servers": [_server(raw) for raw in SERVERS],
        "api_roots": [{"base": base, "root": cloud_api_root(base)} for base in BASES],
        "display_placeholder": activation.display_server_url(activation.PLACEHOLDER_CLOUD_URL),
        "sync_token_name": activation.SYNC_TOKEN_NAME,
        "staged_name": staging.staged_path(database).name,
        "archive_name": staging.archive_path(database, datetime(2026, 9, 25, 18, 7, 3)).name,
    }


def test_the_activation_contract_matches_the_implementation() -> None:
    built = _build()
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == built, (
        "a ativação mudou: regere o contrato e alinhe o C#"
    )
