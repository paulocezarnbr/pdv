"""Ponta a ponta contra a imagem Docker da nuvem, pelo HTTP de verdade.

O que isto prova, que nenhum outro teste prova
----------------------------------------------

`tests/merge.test.ts` roda a regra com o banco dublado.
`tests/integration.test.ts` roda a regra contra o Postgres.

Nenhum dos dois passa por um socket, por um proxy, por um `docker run` — e e
exatamente ai que moram os erros que aparecem no deploy: rota montada no
caminho errado, variavel que o conteiner nao recebeu, cabecalho que o Next
descarta, resposta que o terminal nao sabe ler.

**Escrito em Python de proposito.** O cliente desta API e o PDV, que e Python.
Reescrever o cliente em TypeScript para testar o servidor em TypeScript
testaria as duas metades da mesma cabeca — inclusive os mal-entendidos
compartilhados. Aqui o HMAC da cadeia de auditoria e o do comando sao
recalculados do zero, do jeito que o terminal os calcula.

Como rodar
----------

    # 1. Um Postgres
    docker run -d --name erp-pg-test -p 55432:5432       -e POSTGRES_USER=erp -e POSTGRES_PASSWORD=erp -e POSTGRES_DB=erp       postgres:17-alpine

    # 2. A imagem, construida deste diretorio
    docker build -t erp-cloud-api:test .
    docker run -d --name erp-api-test -p 3111:3000       --add-host=host.docker.internal:host-gateway       -e ADMIN_DATABASE_URL="postgres://erp:erp@host.docker.internal:55432/erp"       -e DATABASE_URL="postgres://erp_app:senha-de-teste-do-app-12345@host.docker.internal:55432/erp"       -e APP_DB_PASSWORD="senha-de-teste-do-app-12345"       -e SESSION_SECRET="chave-de-sessao-de-teste"       erp-cloud-api:test

    # 3. Este script
    python scripts/e2e.py

Sai com codigo 1 na primeira verificacao que falhar.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:3111"
PG = ["docker", "exec", "erp-pg-test", "psql", "-U", "erp", "-d", "erp", "-tAc"]
# Um segredo por execucao: cada rodada cria um terminal novo, e reusar o
# mesmo segredo mascararia um erro de "qual terminal assinou isto".
SECRET = uuid.uuid4().bytes + uuid.uuid4().bytes


def psql(sql: str) -> str:
    result = subprocess.run([*PG, sql], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"psql falhou: {result.stderr}")
    # `INSERT ... RETURNING` imprime o valor e, em seguida, "INSERT 0 1".
    # So a primeira linha e o dado.
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def http(method: str, path: str, body=None, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return error.code, {"raw": raw.decode("utf-8", "replace")[:300]}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok     ' if condition else 'FALHOU '} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        sys.exit(1)


print("== E2E da nuvem, pela imagem Docker ==\n")

# -- preparo: um tenant, uma loja, um código de ativação --------------------- #
tenant = psql("INSERT INTO tenants (name) VALUES ('E2E') RETURNING id")
store = psql(f"INSERT INTO stores (tenant_id, name) VALUES ('{tenant}', 'Loja E2E') RETURNING id")
device = psql("SELECT gen_random_uuid()")
# Codigo novo a cada execucao: o hash e a chave primaria, e um codigo fixo
# faria a segunda rodada falhar por duplicata — um script de diagnostico que so
# roda uma vez nao serve para diagnosticar nada.
CODE = uuid.uuid4().hex[:8].upper()
psql(
    "INSERT INTO device_activation_codes (code_hash, tenant_id, store_id, device_id, label) "
    f"VALUES (encode(sha256('{CODE}'::bytea), 'hex'), "
    f"'{tenant}', '{store}', '{device}', 'Caixa 1')"
)
print(f"  tenant {tenant[:8]}…  loja {store[:8]}…  terminal {device[:8]}…\n")

# -- 1. ativação -------------------------------------------------------------- #
print("[1] ativação do terminal")
status, activation = http(
    "POST",
    "/api/devices/activate",
    {
        "activation_code": CODE,
        "fingerprint": {"hostname": "CAIXA-01", "os": "Windows 11", "arch": "x64"},
        "device_secret_hex": SECRET.hex(),
    },
)
check("o código vira token", status == 200, str(activation)[:120])
token = activation["sync_token"]
check("o tenant volta do servidor", activation["tenant_id"] == tenant)
check("a loja volta nomeada", activation["store_name"] == "Loja E2E")

status, again = http(
    "POST", "/api/devices/activate", {"activation_code": CODE}
)
check("o código é de uso único", status == 410, str(again)[:80])

auth = {"Authorization": f"Bearer {token}"}

# -- 2. sincronização --------------------------------------------------------- #
print("\n[2] sincronização de um lote")

order_uuid = str(uuid.uuid4())
order_id = str(uuid.uuid4())
payment_uuid = str(uuid.uuid4())


def chain(seq: int, prev: str, payload_json: str, created: str) -> dict:
    material = f"{prev}|{seq}|sale_closed|{payload_json}|{created}"
    digest = hmac.new(SECRET, material.encode(), hashlib.sha256).hexdigest()
    return {
        "id": str(uuid.uuid4()),
        "seq": seq,
        "event_type": "sale_closed",
        "severity": "info",
        "actor_user_id": "ana",
        "payload_json": payload_json,
        "prev_hash": prev,
        "hash": digest,
        "created_at": created,
    }


audit = chain(1, "genesis", '{"total_cents":2100}', "2026-09-19T21:30:00+00:00")
batch = {
    "device_id": device,
    "tenant_id": tenant,
    "store_id": store,
    "items": [
        {
            "entity_table": "orders",
            "entity_id": order_id,
            "client_uuid": order_uuid,
            "operation": "insert",
            "payload": {
                "id": order_id,
                "local_number": 1,
                "channel": "waiter",
                "status": "paid",
                "total_cents": 2100,
                "tip_cents": 300,
                "customer_id": "Mesa 4",
                "operator_id": "joao",
                "opened_at": "2026-09-19T20:00:00+00:00",
                "closed_at": "2026-09-19T21:30:00+00:00",
            },
        },
        {
            "entity_table": "payments",
            "entity_id": str(uuid.uuid4()),
            "client_uuid": payment_uuid,
            "operation": "insert",
            "payload": {
                "id": str(uuid.uuid4()),
                "order_id": order_id,
                "method": "cash",
                "amount_cents": 2400,
                "change_cents": 0,
            },
        },
        {
            "entity_table": "audit_ledger",
            "entity_id": audit["id"],
            "client_uuid": str(uuid.uuid4()),
            "operation": "insert",
            "payload": audit,
        },
    ],
}

key = str(uuid.uuid4())
status, pushed = http("POST", "/api/sync/push", batch, {**auth, "Idempotency-Key": key})
check("o lote entra", status == 200 and pushed["applied"] == 3, str(pushed)[:200])

status, replay = http("POST", "/api/sync/push", batch, {**auth, "Idempotency-Key": key})
check(
    "a mesma Idempotency-Key devolve a mesma resposta",
    status == 200 and replay["applied"] == 3,
    "reprocessar faria o terminal achar que a venda nao entrou",
)

status, resent = http(
    "POST", "/api/sync/push", batch, {**auth, "Idempotency-Key": str(uuid.uuid4())}
)
check(
    "reenviar com chave nova vira duplicata, nao venda nova",
    status == 200 and resent["duplicates"] == 3,
    str(resent)[:120],
)

check(
    "a venda esta no banco uma vez so",
    psql(f"SELECT count(*) FROM orders WHERE client_uuid = '{order_uuid}'") == "1",
)
check(
    "a gorjeta chegou fora do total",
    psql(f"SELECT total_cents || '/' || tip_cents FROM orders WHERE client_uuid = '{order_uuid}'")
    == "2100/300",
)

# -- 3. o lote sem Idempotency-Key ------------------------------------------- #
status, _ = http("POST", "/api/sync/push", batch, auth)
check("sem Idempotency-Key o lote e recusado", status == 400)

# -- 4. tenant forjado no corpo ----------------------------------------------- #
forged = {**batch, "tenant_id": str(uuid.uuid4())}
status, _ = http(
    "POST", "/api/sync/push", forged, {**auth, "Idempotency-Key": str(uuid.uuid4())}
)
check("tenant divergente do token e 403", status == 403)

# -- 5. reescrita da auditoria ancorada --------------------------------------- #
print("\n[3] a tentativa de reescrever historia")
adulterado = chain(1, "genesis", '{"total_cents":1}', "2026-09-19T21:30:00+00:00")
status, tampered = http(
    "POST",
    "/api/sync/push",
    {
        **batch,
        "items": [
            {
                "entity_table": "audit_ledger",
                "entity_id": adulterado["id"],
                "client_uuid": str(uuid.uuid4()),
                "operation": "insert",
                "payload": adulterado,
            }
        ],
    },
    {**auth, "Idempotency-Key": str(uuid.uuid4())},
)
check("o seq ja ancorado com outro conteudo e recusado", tampered["rejected"] == 1, str(tampered)[:150])
check(
    "e vira alerta de fraude",
    psql(f"SELECT count(*) FROM fraud_alerts WHERE tenant_id = '{tenant}'") == "1",
)
check(
    "o valor original continua la",
    psql(
        f"SELECT payload_json FROM audit_ledger WHERE tenant_id = '{tenant}' AND seq = 1"
    )
    == '{"total_cents":2100}',
)

# -- 6. token invalido --------------------------------------------------------- #
status, _ = http("GET", "/api/commands/pending", None, {"Authorization": "Bearer nada"})
check("token desconhecido e 401", status == 401)

# -- 7. comando do painel ------------------------------------------------------ #
print("\n[4] comando do painel ate o terminal")
from_panel = psql(
    "INSERT INTO panel_users (tenant_id, email, name, role, password_hash, "
    "can_authorize, max_discount_percent) VALUES "
    f"('{tenant}', 'gerente@e2e.test', 'Bruno Gerente', 'owner', 'x', TRUE, 30) "
    "RETURNING id"
)
session_token = uuid.uuid4().hex + uuid.uuid4().hex
session_hash = hashlib.sha256(session_token.encode()).hexdigest()
psql(
    "INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at) "
    f"VALUES ('{session_hash}', '{tenant}', '{from_panel}', now() + interval '1 hour')"
)

cookie = {"Cookie": f"erp_session={session_token}"}
command_uuid = str(uuid.uuid4())
status, issued = http(
    "POST",
    "/api/commands/issue",
    {
        "device_id": device,
        "kind": "apply_discount",
        "command_uuid": command_uuid,
        "payload": {"order_id": order_id, "percent": "10", "reason": "Atraso na cozinha"},
    },
    cookie,
)
check("o painel emite o comando", status == 200, str(issued)[:150])

expected = hmac.new(
    SECRET,
    "\x1f".join(
        (
            command_uuid,
            device,
            "apply_discount",
            json.dumps(
                {"order_id": order_id, "percent": "10", "reason": "Atraso na cozinha"},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            issued["issued_at"],
        )
    ).encode(),
    hashlib.sha256,
).hexdigest()
check(
    "a assinatura bate com a que o terminal calcularia",
    issued["signature"] == expected,
    "divergir aqui faz o terminal recusar comando legitimo",
)

status, over = http(
    "POST",
    "/api/commands/issue",
    {
        "device_id": device,
        "kind": "apply_discount",
        "command_uuid": str(uuid.uuid4()),
        "payload": {"order_id": order_id, "percent": "80", "reason": "porque sim"},
    },
    cookie,
)
check("acima do teto do perfil e recusado", status == 403, str(over)[:100])

status, pending = http("GET", "/api/commands/pending", None, auth)
check("o terminal recebe o comando", len(pending["commands"]) == 1)

status, pending_again = http("GET", "/api/commands/pending", None, auth)
check(
    "entregar nao consome",
    len(pending_again["commands"]) == 1,
    "perder em silencio e pior que entregar duas vezes",
)

status, reported = http(
    "POST",
    "/api/commands/results",
    {
        "tenant_id": tenant,
        "store_id": store,
        "device_id": device,
        "results": [
            {"command_uuid": command_uuid, "status": "applied", "message": "aplicado"}
        ],
    },
    auth,
)
check("o terminal relata o que fez", reported["accepted"] == [command_uuid])

status, after = http("GET", "/api/commands/pending", None, auth)
check("relatado sai da fila", len(after["commands"]) == 0)

# -- 8. cursor de download ----------------------------------------------------- #
print("\n[5] download de cadastro")
psql(
    f"INSERT INTO products (tenant_id, sku, name, price_cents) VALUES "
    f"('{tenant}', 'CAFE', 'Cafe expresso', 700)"
)
status, pull = http("GET", "/api/sync/pull?entity_table=products&since=0", None, auth)
check("o terminal baixa o cardapio", status == 200 and len(pull["rows"]) == 1, str(pull)[:150])
check("com cursor para o proximo ciclo", pull["last_server_seq"] > 0)

status, _ = http("GET", "/api/sync/pull?entity_table=panel_users&since=0", None, auth)
check("tabela fora da lista branca e recusada", status == 400)

print("\nTODAS AS VERIFICACOES PASSARAM.")
