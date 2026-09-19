/**
 * As quatro regras do `SyncMerger`, sem banco.
 *
 * O Postgres é substituído por um dublê que implementa só o que o merger usa:
 * a consulta da âncora, a consulta do ledger, o `INSERT ... ON CONFLICT` e o
 * alerta. Não é preguiça de subir um banco — é o que permite testar a **regra**
 * (o que acontece quando o seq pula, quando o hash diverge, quando o prev_hash
 * não bate) sem que o teste dependa de um contêiner de Postgres estar de pé.
 *
 * O que um teste com banco de verdade acrescentaria — que o índice único
 * `(tenant_id, client_uuid)` existe e que o `ON CONFLICT` casa com ele — está
 * coberto por `integration.test.ts`, que roda quando há `DATABASE_URL`.
 */

import { describe, expect, it } from "vitest";

import { computeChainHash } from "../src/lib/crypto/audit.ts";
import { SyncMerger, type SyncItem } from "../src/lib/sync/merge.ts";

const TENANT = "11111111-1111-1111-1111-111111111111";
const STORE = "22222222-2222-2222-2222-222222222222";
const DEVICE = "33333333-3333-3333-3333-333333333333";
const SECRET = Buffer.from("segredo-de-teste-do-terminal", "utf8");

/** Estado do "banco" que o dublê guarda, para o teste inspecionar. */
interface FakeState {
  anchor: { last_seq: number; last_hash: string } | null;
  ledger: Map<number, string>;
  inserted: { table: string; row: Record<string, unknown> }[];
  alerts: { reason: string; detail: unknown }[];
  nextSeq: number;
}

function fakeTx(state: FakeState) {
  const tagged = (strings: TemplateStringsArray, ...values: unknown[]) => {
    const query = strings.join("?").replace(/\s+/g, " ").trim();

    if (query.startsWith("SELECT last_seq")) {
      return Promise.resolve(
        state.anchor
          ? [{ last_seq: String(state.anchor.last_seq), last_hash: state.anchor.last_hash }]
          : [],
      );
    }
    if (query.startsWith("SELECT hash FROM audit_ledger")) {
      const seq = Number(values[2]);
      const hash = state.ledger.get(seq);
      return Promise.resolve(hash ? [{ hash }] : []);
    }
    if (query.startsWith("INSERT INTO device_anchors")) {
      state.anchor = { last_seq: Number(values[2]), last_hash: String(values[3]) };
      return Promise.resolve([]);
    }
    if (query.startsWith("INSERT INTO fraud_alerts")) {
      state.alerts.push({ reason: String(values[3]), detail: values[4] });
      return Promise.resolve([]);
    }
    throw new Error(`consulta não prevista pelo dublê: ${query}`);
  };

  const tx = tagged as unknown as Record<string, unknown>;
  tx["json"] = (value: unknown) => value;
  tx["unsafe"] = (sqlText: string, params: unknown[]) => {
    const table = /INSERT INTO (\w+)/.exec(sqlText)?.[1] ?? "?";
    const columns = [...sqlText.matchAll(/"(\w+)"/g)].map((m) => m[1] as string);
    const row = Object.fromEntries(columns.map((c, i) => [c, params[i]]));

    // Idempotência: o mesmo `client_uuid` não entra duas vezes. É o que o
    // índice único faz no Postgres de verdade.
    const uuid = String(row["client_uuid"]);
    if (state.inserted.some((i) => String(i.row["client_uuid"]) === uuid)) {
      return Promise.resolve([]);
    }
    state.inserted.push({ table, row });
    if (table === "audit_ledger") {
      state.ledger.set(Number(row["seq"]), String(row["hash"]));
    }
    return Promise.resolve([{ server_seq: String(state.nextSeq++) }]);
  };
  return { tx, state };
}

function freshState(): FakeState {
  return { anchor: null, ledger: new Map(), inserted: [], alerts: [], nextSeq: 1 };
}

function merger() {
  return new SyncMerger({
    tenantId: TENANT,
    storeId: STORE,
    deviceId: DEVICE,
    secret: SECRET,
  });
}

function auditItem(
  seq: number,
  overrides: Partial<Record<string, unknown>> = {},
  { corrupt = false } = {},
): SyncItem {
  const payload: Record<string, unknown> = {
    id: `audit-${seq}`,
    seq,
    event_type: "sale_closed",
    severity: "info",
    actor_user_id: "ana",
    payload_json: `{"total":${seq * 100}}`,
    prev_hash: seq === 1 ? "genesis" : `hash-${seq - 1}`,
    created_at: "2026-09-19T12:00:00+00:00",
    ...overrides,
  };
  payload["hash"] = corrupt
    ? "0".repeat(64)
    : computeChainHash(SECRET, {
        prevHash: String(payload["prev_hash"]),
        seq,
        eventType: String(payload["event_type"]),
        payloadJson: String(payload["payload_json"]),
        createdAt: String(payload["created_at"]),
      });

  return {
    entity_table: "audit_ledger",
    entity_id: `audit-${seq}`,
    client_uuid: `cu-audit-${seq}`,
    operation: "insert",
    payload,
  };
}

function order(uuid: string, total = 1500): SyncItem {
  return {
    entity_table: "orders",
    entity_id: `order-${uuid}`,
    client_uuid: uuid,
    operation: "insert",
    payload: {
      id: `order-${uuid}`,
      local_number: 1,
      status: "paid",
      total_cents: total,
      tip_cents: 0,
    },
  };
}

// --------------------------------------------------------------------------- //

describe("regra 1 — idempotência por client_uuid", () => {
  it("o reenvio vira duplicata, não uma segunda venda", async () => {
    const { tx, state } = fakeTx(freshState());
    const item = order("cu-1");

    const first = await merger().apply([item], tx as never);
    const second = await merger().apply([item], tx as never);

    expect(first[0]?.status).toBe("applied");
    expect(second[0]?.status).toBe("duplicate");
    expect(state.inserted).toHaveLength(1);
  });

  it("o applied devolve o server_seq, que é o cursor do cliente", async () => {
    const { tx } = fakeTx(freshState());

    const [result] = await merger().apply([order("cu-1")], tx as never);

    expect(result?.server_seq).toBeTypeOf("number");
  });
});

describe("o tenant vem do contexto, nunca do payload", () => {
  it("um tenant forjado no corpo é descartado", async () => {
    const { tx, state } = fakeTx(freshState());
    const forjado = order("cu-1");
    forjado.payload["tenant_id"] = "99999999-9999-9999-9999-999999999999";

    await merger().apply([forjado], tx as never);

    expect(state.inserted[0]?.row["tenant_id"]).toBe(TENANT);
  });

  it("colunas fora da lista branca não entram", async () => {
    const { tx, state } = fakeTx(freshState());
    const item = order("cu-1");
    // `server_seq` e `received_at` são do servidor. Aceitá-los do cliente
    // deixaria um terminal reescrever o próprio cursor e o carimbo de chegada.
    item.payload["server_seq"] = 999_999;
    item.payload["received_at"] = "1999-01-01T00:00:00Z";

    await merger().apply([item], tx as never);

    expect(state.inserted[0]?.row).not.toHaveProperty("server_seq");
    expect(state.inserted[0]?.row).not.toHaveProperty("received_at");
  });

  it("uma tabela fora da lista é recusada, não criada", async () => {
    const { tx, state } = fakeTx(freshState());

    const [result] = await merger().apply(
      [{ ...order("cu-1"), entity_table: "panel_users" }],
      tx as never,
    );

    expect(result?.status).toBe("rejected");
    expect(result?.message).toContain("panel_users");
    expect(state.inserted).toHaveLength(0);
  });
});

describe("regra 3 — a cadeia é recalculada, não acreditada", () => {
  it("o elo legítimo entra e vira a nova âncora", async () => {
    const { tx, state } = fakeTx(freshState());

    const [result] = await merger().apply([auditItem(1)], tx as never);

    expect(result?.status).toBe("applied");
    expect(state.anchor?.last_seq).toBe(1);
  });

  it("um hash forjado é recusado e vira alerta", async () => {
    const { tx, state } = fakeTx(freshState());

    const [result] = await merger().apply(
      [auditItem(1, {}, { corrupt: true })],
      tx as never,
    );

    expect(result?.status).toBe("rejected");
    expect(result?.message).toContain("HMAC");
    expect(state.alerts).toHaveLength(1);
  });

  it("conteúdo alterado com hash antigo não passa", async () => {
    // O caso real: alguém edita o `payload_json` no SQLite da loja para baixar
    // o total, e deixa o hash como estava.
    const { tx, state } = fakeTx(freshState());
    const item = auditItem(1);
    item.payload["payload_json"] = '{"total":1}';

    const [result] = await merger().apply([item], tx as never);

    expect(result?.status).toBe("rejected");
    expect(state.inserted).toHaveLength(0);
  });
});

describe("regra 4 — marca d'água alta", () => {
  it("reenviar o mesmo seq com o mesmo hash é duplicata benigna", async () => {
    const { tx } = fakeTx(freshState());
    const item = auditItem(1);
    await merger().apply([item], tx as never);

    const [again] = await merger().apply([item], tx as never);

    expect(again?.status).toBe("duplicate");
  });

  it("reenviar o mesmo seq com hash diferente é fraude", async () => {
    // Este é O cenário que o sistema inteiro existe para pegar: a venda já
    // ancorada na nuvem sendo reescrita por quem controla o PC da loja.
    const { tx, state } = fakeTx(freshState());
    await merger().apply([auditItem(1)], tx as never);

    const reescrito = auditItem(1, { payload_json: '{"total":1}' });
    const [result] = await merger().apply([reescrito], tx as never);

    expect(result?.status).toBe("rejected");
    expect(state.alerts.at(-1)?.reason).toContain("Reescrita");
  });

  it("um buraco na sequência é recusado e alertado", async () => {
    // O terminal pulou entradas: ou houve perda, ou alguém apagou o rastro
    // local antes de sincronizar.
    const { tx, state } = fakeTx(freshState());
    await merger().apply([auditItem(1)], tx as never);

    const [result] = await merger().apply([auditItem(5)], tx as never);

    expect(result?.status).toBe("rejected");
    expect(result?.message).toContain("fora de ordem");
    expect(state.alerts.at(-1)?.reason).toContain("Buraco");
  });

  it("um elo que não aponta para o anterior é recusado", async () => {
    const { tx, state } = fakeTx(freshState());
    await merger().apply([auditItem(1)], tx as never);
    const anchored = state.anchor!.last_hash;

    const orfao = auditItem(2, { prev_hash: "outro-galho" });
    const [result] = await merger().apply([orfao], tx as never);

    expect(result?.status).toBe("rejected");
    expect(result?.message).toContain("prev_hash");
    expect(state.anchor?.last_hash).toBe(anchored);
  });
});

describe("ordem de aplicação", () => {
  it("a auditoria é aplicada por seq, mesmo se o lote vier embaralhado", async () => {
    // Um retry parcial remonta o lote fora de ordem. Sem a ordenação, o elo 2
    // chegaria antes do 1 e seria recusado por "buraco" — uma rejeição por
    // motivo que não é adulteração nenhuma.
    const { tx, state } = fakeTx(freshState());
    const um = auditItem(1);
    const dois = auditItem(2, { prev_hash: um.payload["hash"] });

    const results = await merger().apply([dois, um], tx as never);

    expect(results.map((r) => r.status)).toEqual(["applied", "applied"]);
    expect(state.anchor?.last_seq).toBe(2);
  });

  it("a resposta volta na ordem em que o cliente enviou", async () => {
    const { tx } = fakeTx(freshState());
    const um = auditItem(1);
    const dois = auditItem(2, { prev_hash: um.payload["hash"] });

    const results = await merger().apply([dois, um], tx as never);

    expect(results.map((r) => r.client_uuid)).toEqual([
      dois.client_uuid,
      um.client_uuid,
    ]);
  });
});
