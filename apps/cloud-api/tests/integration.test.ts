/**
 * As regras contra um Postgres de verdade.
 *
 * `merge.test.ts` testa a **regra** com o banco dublado, e isso cobre a lógica.
 * O que ele não pode cobrir é o que só existe no banco:
 *
 * * que o índice único `(tenant_id, client_uuid)` realmente existe, e que o
 *   `ON CONFLICT` casa com ele. Um `ON CONFLICT` sem índice correspondente não
 *   dedupa nada — ele levanta erro, e o dublê nunca diria isso;
 * * que as colunas da lista branca existem com esses nomes em `001_init.sql`.
 *   Um typo no nome de coluna passa pelo dublê e derruba a rota em produção;
 * * que a transação do lote desfaz tudo quando um item falha.
 *
 * Pula quando não há `TEST_DATABASE_URL`, e isso é deliberado: um teste que
 * exige Postgres não pode impedir alguém de rodar a suíte na máquina dele. O
 * CI define a variável, e é lá que ele não pode pular.
 *
 *     docker run -d -p 55432:5432 -e POSTGRES_USER=erp -e POSTGRES_PASSWORD=erp \
 *       -e POSTGRES_DB=erp postgres:17-alpine
 *     TEST_DATABASE_URL=postgres://erp:erp@localhost:55432/erp npx vitest run
 */

import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { computeChainHash } from "../src/lib/crypto/audit.ts";
import { signCommand } from "../src/lib/crypto/commands.ts";
import { SyncMerger, type SyncItem } from "../src/lib/sync/merge.ts";

const URL = process.env.TEST_DATABASE_URL;
const SECRET = Buffer.from("segredo-de-teste-do-terminal", "utf8");

const describeDb = URL ? describe : describe.skip;

describeDb("contra um Postgres de verdade", () => {
  let sql: postgres.Sql;
  let tenant: string;
  let store: string;
  let device: string;

  beforeAll(async () => {
    sql = postgres(URL!, { max: 2, onnotice: () => {} });

    // Um tenant novo por execução: os testes escrevem de verdade, e reusar o
    // mesmo faria a segunda execução falhar por duplicata — mascarando erro
    // real de idempotência como "erro de teste sujo".
    const [t] = await sql<{ id: string }[]>`
      INSERT INTO tenants (name) VALUES (${"Teste " + randomUUID()}) RETURNING id
    `;
    tenant = t!.id;

    const [s] = await sql<{ id: string }[]>`
      INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Loja de teste')
      RETURNING id
    `;
    store = s!.id;

    device = randomUUID();
    await sql`
      INSERT INTO devices (id, tenant_id, store_id, token_hash)
      VALUES (${device}, ${tenant}, ${store}, ${randomUUID()})
    `;
    await sql`
      INSERT INTO device_secrets (tenant_id, device_id, secret)
      VALUES (${tenant}, ${device}, ${SECRET})
    `;
  });

  afterAll(async () => {
    if (!sql) return;
    // CASCADE limpa tudo que aponta para o tenant. Os dados de movimento não
    // têm FK para `tenants` de propósito (o terminal envia antes de qualquer
    // join ser possível), então saem à mão.
    await sql`DELETE FROM audit_ledger WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM order_items WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM payments WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM orders WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM device_anchors WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM fraud_alerts WHERE tenant_id = ${tenant}`;
    await sql`DELETE FROM tenants WHERE id = ${tenant}`;
    await sql.end({ timeout: 5 });
  });

  function merger() {
    return new SyncMerger({
      tenantId: tenant,
      storeId: store,
      deviceId: device,
      secret: SECRET,
    });
  }

  function orderItem(uuid: string, total = 2100): SyncItem {
    return {
      entity_table: "orders",
      entity_id: randomUUID(),
      client_uuid: uuid,
      operation: "insert",
      payload: {
        id: randomUUID(),
        local_number: 7,
        channel: "waiter",
        status: "paid",
        total_cents: total,
        tip_cents: 300,
        customer_id: "Mesa 4",
        operator_id: "joao",
        opened_at: "2026-09-19T20:00:00+00:00",
        closed_at: "2026-09-19T21:30:00+00:00",
      },
    };
  }

  it("o ON CONFLICT casa com o índice — o reenvio não duplica a venda", async () => {
    const uuid = randomUUID();

    const first = await sql.begin((tx) => merger().apply([orderItem(uuid)], tx));
    const second = await sql.begin((tx) => merger().apply([orderItem(uuid)], tx));

    expect(first[0]?.status).toBe("applied");
    expect(second[0]?.status).toBe("duplicate");

    const rows = await sql`
      SELECT id FROM orders WHERE tenant_id = ${tenant} AND client_uuid = ${uuid}
    `;
    expect(rows).toHaveLength(1);
  });

  it("as colunas da lista branca existem no schema", async () => {
    // Um typo num nome de coluna passa pelo dublê e derruba a rota em
    // produção, na primeira venda depois do deploy.
    const uuid = randomUUID();

    const [result] = await sql.begin((tx) =>
      merger().apply([orderItem(uuid, 4200)], tx),
    );

    expect(result?.status).toBe("applied");
    const [row] = await sql<{ total_cents: string; tip_cents: string }[]>`
      SELECT total_cents, tip_cents FROM orders
       WHERE tenant_id = ${tenant} AND client_uuid = ${uuid}
    `;
    expect(Number(row!.total_cents)).toBe(4200);
    // A gorjeta chega e fica fora do total, como no terminal.
    expect(Number(row!.tip_cents)).toBe(300);
  });

  it("o server_seq cresce e serve de cursor", async () => {
    const [a] = await sql.begin((tx) =>
      merger().apply([orderItem(randomUUID())], tx),
    );
    const [b] = await sql.begin((tx) =>
      merger().apply([orderItem(randomUUID())], tx),
    );

    expect(b!.server_seq!).toBeGreaterThan(a!.server_seq!);
  });

  it("a cadeia de auditoria ancora e o reenvio adulterado é recusado", async () => {
    const link = (seq: number, prev: string, total: number) => {
      const payload: Record<string, unknown> = {
        id: randomUUID(),
        seq,
        event_type: "sale_closed",
        severity: "info",
        actor_user_id: "ana",
        payload_json: `{"total":${total}}`,
        prev_hash: prev,
        created_at: "2026-09-19T21:30:00+00:00",
      };
      payload["hash"] = computeChainHash(SECRET, {
        prevHash: prev,
        seq,
        eventType: "sale_closed",
        payloadJson: String(payload["payload_json"]),
        createdAt: String(payload["created_at"]),
      });
      return {
        entity_table: "audit_ledger",
        entity_id: String(payload["id"]),
        client_uuid: `cu-${seq}-${total}`,
        operation: "insert" as const,
        payload,
      };
    };

    const um = link(1, "genesis", 2100);
    const dois = link(2, String(um.payload["hash"]), 4200);

    const applied = await sql.begin((tx) => merger().apply([um, dois], tx));
    expect(applied.map((r) => r.status)).toEqual(["applied", "applied"]);

    const [anchor] = await sql<{ last_seq: string }[]>`
      SELECT last_seq FROM device_anchors
       WHERE tenant_id = ${tenant} AND device_id = ${device}
    `;
    expect(Number(anchor!.last_seq)).toBe(2);

    // Agora o ataque: reescrever o seq 1 com um total menor. É o cenário que o
    // sistema inteiro existe para pegar.
    const reescrito = link(1, "genesis", 1);
    const [result] = await sql.begin((tx) => merger().apply([reescrito], tx));

    expect(result?.status).toBe("rejected");

    const alerts = await sql<{ reason: string }[]>`
      SELECT reason FROM fraud_alerts WHERE tenant_id = ${tenant}
    `;
    expect(alerts.some((a) => a.reason.includes("Reescrita"))).toBe(true);

    // E o valor original continua lá: ancorado é inalcançável.
    const [original] = await sql<{ payload_json: string }[]>`
      SELECT payload_json FROM audit_ledger
       WHERE tenant_id = ${tenant} AND device_id = ${device} AND seq = 1
    `;
    expect(original!.payload_json).toBe('{"total":2100}');
  });

  it("item que o banco recusa vai sozinho para a quarentena", async () => {
    // Antes, o lote inteiro caía — e com ele todo lote seguinte, porque o
    // terminal reenvia em ordem: uma linha inválida parava a loja. Agora a
    // linha é recusada com o motivo, e o resto do lote entra. Falha do BANCO
    // (conexão, disco) continua derrubando o lote inteiro: essa o reenvio
    // resolve, e o teste de unidade cobre.
    const bom = orderItem(randomUUID());
    const ruim: SyncItem = {
      entity_table: "orders",
      entity_id: randomUUID(),
      client_uuid: randomUUID(),
      operation: "insert",
      // `local_number` é NOT NULL INTEGER; o texto abaixo estoura no banco.
      payload: { id: randomUUID(), local_number: "não é número" },
    };

    const results = await sql.begin((tx) => merger().apply([bom, ruim], tx));

    expect(results.map((r) => r.status)).toEqual(["applied", "rejected"]);
    expect(results[1]!.message).toContain("22P02");
    const rows = await sql`
      SELECT id FROM orders
       WHERE tenant_id = ${tenant} AND client_uuid = ${bom.client_uuid}
    `;
    expect(rows).toHaveLength(1);
  });

  it("um comando é assinado com o segredo daquele terminal", async () => {
    // Não é sobre o HMAC (isso o teste de contrato cobre): é sobre o segredo
    // sair da tabela certa, do terminal certo, no tenant certo.
    const [row] = await sql<{ secret: Buffer }[]>`
      SELECT secret FROM device_secrets
       WHERE tenant_id = ${tenant} AND device_id = ${device}
    `;

    const signature = signCommand(Buffer.from(row!.secret), {
      commandUuid: "cmd-1",
      deviceId: device,
      kind: "apply_discount",
      payload: { percent: "10", reason: "Atraso" },
      issuedAt: "2026-09-19T12:00:00+00:00",
    });

    expect(signature).toBe(
      signCommand(SECRET, {
        commandUuid: "cmd-1",
        deviceId: device,
        kind: "apply_discount",
        payload: { percent: "10", reason: "Atraso" },
        issuedAt: "2026-09-19T12:00:00+00:00",
      }),
    );
  });

  it("o RLS barra a leitura de outro tenant", async () => {
    // A aplicação já filtra por tenant em toda consulta. O RLS existe porque
    // "já filtra" depende de ninguém esquecer, e esquecer um WHERE numa
    // consulta nova é o erro que vaza faturamento de um cliente para outro.
    //
    // A conexão aqui é pelo papel `erp_app`, e isso NÃO é detalhe de teste: é
    // a única forma de a política valer. O usuário administrativo — que é o
    // que o Coolify cria por padrão — é superusuário, e superusuário tem
    // `rolbypassrls`: ignora toda política, com ou sem FORCE. Foi assim que
    // este teste pegou o RLS inerte na primeira vez que rodou.
    const appUrl = process.env.TEST_APP_DATABASE_URL;
    if (!appUrl) {
      // Sem pular em silêncio: um teste que passa sem testar é pior que um
      // teste ausente, porque o verde diz que a barreira existe.
      throw new Error(
        "TEST_APP_DATABASE_URL não definida. Este teste precisa conectar pelo " +
          "papel `erp_app` (não-superusuário) — conectado como administrador, " +
          "o RLS é ignorado e o teste passaria sem provar nada.",
      );
    }

    const app = postgres(appUrl, { max: 1, onnotice: () => {} });
    try {
      // Confirma a premissa antes de confiar no resultado: se este papel
      // pudesse ignorar RLS, o `toHaveLength(0)` abaixo não significaria nada.
      const [role] = await app<{ bypasses: boolean }[]>`
        SELECT (rolsuper OR rolbypassrls) AS bypasses
          FROM pg_roles WHERE rolname = current_user
      `;
      expect(role!.bypasses).toBe(false);

      const outro = randomUUID();
      const visible = await app.begin(async (tx) => {
        await tx`SELECT set_config('app.tenant_id', ${outro}, true)`;
        // Sem o WHERE de propósito: é justamente o esquecimento que o RLS cobre.
        return tx<{ id: string }[]>`SELECT id FROM orders`;
      });

      expect(visible).toHaveLength(0);

      // E com o tenant certo, os dados aparecem — senão "barrou tudo" passaria
      // por "isolou", e uma política quebrada pareceria uma política correta.
      const meus = await app.begin(async (tx) => {
        await tx`SELECT set_config('app.tenant_id', ${tenant}, true)`;
        return tx<{ id: string }[]>`SELECT id FROM orders`;
      });
      expect(meus.length).toBeGreaterThan(0);
    } finally {
      await app.end({ timeout: 5 });
    }
  });

  it("a aplicação não consegue alterar nem apagar o ledger", async () => {
    // O ledger é o destino inalcançável do sistema inteiro. A lista branca do
    // `SyncMerger` já impede escrever onde não deve; esta é a barreira abaixo
    // dela, para o dia em que um bug escapar da lista.
    const appUrl = process.env.TEST_APP_DATABASE_URL;
    if (!appUrl) throw new Error("TEST_APP_DATABASE_URL não definida.");

    const app = postgres(appUrl, { max: 1, onnotice: () => {} });
    try {
      await expect(
        app`UPDATE audit_ledger SET payload_json = '{}'`,
      ).rejects.toThrow(/permission denied|permissão negada/i);
      await expect(app`DELETE FROM audit_ledger`).rejects.toThrow(
        /permission denied|permissão negada/i,
      );
    } finally {
      await app.end({ timeout: 5 });
    }
  });
});
