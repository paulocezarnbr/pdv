"""O push contra a nuvem REAL — o que o caixa manda precisa caber lá.

O dia de caixa de `push_day.py` vira `contracts/push-day.json`, e a nuvem
aplica esse arquivo no `SyncMerger` real contra Postgres
(`apps/cloud-api/tests/push-contract.integration.test.ts`). Aqui ficam as três
garantias do lado de cá:

1. **O arquivo é o caixa de hoje.** Mudou um payload, o teste falha até o
   arquivo ser regerado — e aí a nuvem o testa de novo.
2. **Todo ponto que enfileira aparece no dia.** Um `enqueue` novo que o dia
   não exercita seria um formato que a nuvem nunca viu.
3. **Nada que sai daqui some lá sem decisão.** A nuvem descarta em silêncio o
   que não está na lista branca dela — foi assim que os insumos consumidos
   sumiram e o CMV do painel ficou em zero. Cada chave que não entra lá precisa
   estar em `LOCAL_ONLY`, com o motivo.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_push_contract.py``.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

import push_day

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT.parent / "cloud-api"

#: O que o caixa manda e a nuvem, de propósito, não guarda como coluna.
LOCAL_ONLY: dict[str, dict[str, str]] = {
    "*": {
        "tenant_id": "vem do token do terminal, nunca do corpo",
        "origin_device_id": "a nuvem sabe o terminal pelo token",
    },
    "orders": {
        "table_label": "cópia impressa no cupom; a mesa é `table_id`",
        "reason": "o motivo do cancelamento vai na trilha de auditoria",
        "authorized_by_user_id": "quem autorizou vai na trilha de auditoria",
    },
    "order_items": {
        "gross_weight_grams": "bruto e tara ficam no quadro cru da balança",
        "tare_grams": "bruto e tara ficam no quadro cru da balança",
        "ingredients": "vira linhas de `order_item_ingredients` no merger",
    },
    "stock_movements": {
        "qty_mg": "traduzido para `quantity_mg` no merger",
        "movement_type": "traduzido para `reason` no merger",
        "reference_type": "com `reference_id`, vira `order_item_id` no merger",
        "reference_id": "com `reference_type`, vira `order_item_id` no merger",
        "unit_cost_cents": "o custo do consumo fica no insumo do item",
    },
}

#: Colunas obrigatórias lá que a nuvem preenche sozinha.
SERVER_FILLED = {"tenant_id", "store_id", "device_id", "id", "client_uuid",
                 "server_seq", "received_at"}
#: Obrigatórias lá que o merger deriva de outra chave daqui.
ADAPTED = {"stock_movements": {"quantity_mg": "qty_mg"}}


@pytest.fixture(scope="module")
def day(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, object]]:
    items = push_day.run_day(tmp_path_factory.mktemp("dia"))
    if os.environ.get("PDV_UPDATE_CONTRACT") == "1":
        push_day.CONTRACTS.mkdir(exist_ok=True)
        push_day.CURRENT.write_text(
            json.dumps(push_day.document(items, caixa="atual"), ensure_ascii=False, indent=1)
            + "\n",
            encoding="utf-8",
        )
    return items


def _file_items() -> list[dict[str, object]]:
    return json.loads(push_day.CURRENT.read_text(encoding="utf-8"))["items"]


def _writable() -> dict[str, set[str]]:
    source = (CLOUD / "src/lib/sync/merge.ts").read_text(encoding="utf-8")
    block = re.search(r"const WRITABLE[^=]*= \{(.*?)\n\};", source, re.S)
    assert block, "a lista branca da nuvem mudou de forma; ajuste este teste"
    return {
        name: set(re.findall(r'"(\w+)"', columns))
        for name, columns in re.findall(r"(\w+): \[(.*?)\]", block.group(1), re.S)
    }


def _split_columns(body: str) -> list[str]:
    """As definições de um CREATE TABLE, separadas pelas vírgulas de fora."""
    parts, depth, current = [], 0, []
    for char in re.sub(r"--[^\n]*", "", body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def _required_in_cloud(table: str) -> set[str]:
    """NOT NULL sem DEFAULT, olhando TODAS as migrations (inclusive as que soltam)."""
    required: dict[str, bool] = {}
    for path in sorted((CLOUD / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        create = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", text, re.S)
        if create:
            for part in _split_columns(create.group(1)):
                words = part.split()
                if words[0].upper() in {"CONSTRAINT", "UNIQUE", "PRIMARY", "CHECK", "FOREIGN"}:
                    continue
                upper = part.upper()
                required[words[0]] = (
                    ("NOT NULL" in upper or "PRIMARY KEY" in upper) and "DEFAULT" not in upper
                )
        for column in re.findall(
            rf"ALTER TABLE {table} ALTER COLUMN (\w+) DROP NOT NULL", text
        ):
            required[column] = False
    return {column for column, needed in required.items() if needed}


def _enqueue_sites() -> list[tuple[str, str, set[str], set[str]]]:
    """Cada `enqueue(...)` do código: arquivo:linha, tabela, operações, chaves."""
    sites = []
    for path in sorted((ROOT / "src" / "pdv").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "enqueue"):
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            table = kwargs.get("entity_table")
            if not isinstance(table, ast.Constant):
                continue
            operation = kwargs.get("operation")
            operations = {
                n.value for n in ast.walk(operation)
                if isinstance(n, ast.Constant) and n.value in {"insert", "update"}
            } if operation is not None else set()
            payload = kwargs.get("payload")
            keys = {
                k.value for k in getattr(payload, "keys", [])
                if isinstance(k, ast.Constant)
            }
            sites.append((f"{path.relative_to(ROOT)}:{node.lineno}", table.value,
                          operations or {"insert", "update"}, keys))
    return sites


def test_the_contract_file_is_what_the_counter_sends_today(day) -> None:  # noqa: ANN001
    assert push_day.CURRENT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o arquivo"
    assert push_day.shapes(day) == push_day.shapes(_file_items()), (
        "o caixa mudou o que enfileira; regere contracts/push-day.json com "
        "PDV_UPDATE_CONTRACT=1 e rode o teste de contrato da nuvem"
    )


def test_every_place_that_enqueues_shows_up_in_the_day(day) -> None:  # noqa: ANN001
    seen = push_day.shapes(day)
    missing = [
        where
        for where, table, operations, keys in _enqueue_sites()
        if not any(
            t == table and op in operations and keys <= set(shape)
            for t, op, shape in seen
        )
    ]
    assert missing == [], f"estes pontos enfileiram algo que o dia não exercita: {missing}"


def test_nothing_the_counter_sends_is_dropped_without_a_decision(day) -> None:  # noqa: ANN001
    writable = _writable()
    dropped = set()
    for item in day:
        table = str(item["entity_table"])
        assert table in writable, f"a nuvem recusaria a tabela inteira: {table}"
        local = set(LOCAL_ONLY["*"]) | set(LOCAL_ONLY.get(table, {}))
        for key in item["payload"]:  # type: ignore[union-attr]
            if key not in writable[table] and key not in local:
                dropped.add(f"{table}.{key}")
    assert dropped == set(), f"a nuvem descartaria em silêncio: {sorted(dropped)}"


def test_every_column_the_cloud_requires_is_sent(day) -> None:  # noqa: ANN001
    missing = set()
    for item in day:
        if item["operation"] != "insert":
            continue
        table = str(item["entity_table"])
        payload = item["payload"]
        for column in _required_in_cloud(table) - SERVER_FILLED:
            source = ADAPTED.get(table, {}).get(column, column)
            if payload.get(source) is None:  # type: ignore[union-attr]
                missing.add(f"{table}.{column}")
    assert missing == set(), f"a nuvem recusaria o lote inteiro por: {sorted(missing)}"


def test_the_required_column_check_would_have_caught_the_bug() -> None:
    """Sem isto, um parser quebrado passaria o teste de cima por não achar nada."""
    assert "quantity_mg" in _required_in_cloud("stock_movements")
    assert "method" in _required_in_cloud("payments")
    assert "local_number" not in _required_in_cloud("orders")  # solta na 017
