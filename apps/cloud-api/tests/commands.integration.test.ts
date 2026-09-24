/**
 * `POST /api/commands/results` com o aviso de espera — contra Postgres real.
 *
 * O terminal agora conta duas coisas diferentes: o que **decidiu** (`results`)
 * e o que está **parado no caixa** esperando alguém (`awaiting`). O que estes
 * testes guardam é a convivência das duas, porque é nela que um erro apareceria
 * como "o painel parou de mostrar o desconto aplicado":
 *
 * * a espera não tira o comando de `pending` — senão a nuvem pararia de
 *   entregá-lo e o terminal perderia a chance de aplicar depois do aceite;
 * * a espera nunca passa por cima de um resultado já gravado;
 * * terminal antigo, sem o campo, continua relatando como antes;
 * * um terminal não marca espera em comando de outro terminal.
 *
 * Roda com o papel `erp_app` (sem BYPASSRLS), como a aplicação em produção.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

describeDb("resultado e espera de comando remoto", () => {
  let admin: postgres.Sql;
  let POST: (request: Request) => Promise<Response>;
  let tenant: string;
  let store: string;
  let device: string;
  let otherDevice: string;
  const token = `token-terminal-${randomUUID()}`;
  const otherToken = `token-outro-${randomUUID()}`;

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    ({ POST } = await import("../src/app/api/commands/results/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`
      INSERT INTO tenants (name) VALUES (${"Comandos " + randomUUID()}) RETURNING id
    `;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`
      INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Loja Centro') RETURNING id
    `;
    store = s!.id;

    device = randomUUID();
    otherDevice = randomUUID();
    for (const [id, secret] of [[device, token], [otherDevice, otherToken]] as const) {
      await admin`
        INSERT INTO devices (id, tenant_id, store_id, label, token_hash)
        VALUES (${id}, ${tenant}, ${store}, 'Caixa', ${sha256(secret)})
      `;
    }
  });

  afterAll(async () => {
    if (!admin) return;
    await admin`DELETE FROM remote_commands WHERE tenant_id = ${tenant}`;
    await admin`DELETE FROM tenants WHERE id = ${tenant}`;
    await admin.end({ timeout: 5 });
  });

  function sha256(value: string): string {
    return createHash("sha256").update(value, "utf8").digest("hex");
  }

  async function command(target = device): Promise<string> {
    const uuid = randomUUID();
    await admin`
      INSERT INTO remote_commands
        (command_uuid, tenant_id, store_id, device_id, kind, payload_json,
         issued_by_user_id, issued_by_name, signature, status)
      VALUES (${uuid}, ${tenant}, ${store}, ${target}, 'cancel_item', '{}',
              ${randomUUID()}, 'Bruno Gerente', 'assinatura', 'pending')
    `;
    return uuid;
  }

  async function report(
    body: Record<string, unknown>,
    as: { token: string; device: string } = { token, device },
  ): Promise<{ status: number; accepted: string[] }> {
    const response = await POST(
      new Request("http://localhost/api/commands/results", {
        method: "POST",
        headers: {
          authorization: `Bearer ${as.token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          tenant_id: tenant,
          store_id: store,
          device_id: as.device,
          ...body,
        }),
      }),
    );
    const payload = (await response.json()) as { accepted?: string[] };
    return { status: response.status, accepted: payload.accepted ?? [] };
  }

  async function row(uuid: string) {
    const [found] = await admin<{
      status: string;
      awaiting_confirmation_at: Date | null;
      awaiting_message: string | null;
      result_message: string | null;
    }[]>`
      SELECT status, awaiting_confirmation_at, awaiting_message, result_message
        FROM remote_commands WHERE command_uuid = ${uuid}
    `;
    return found!;
  }

  it("a espera fica registrada e o comando continua pendente", async () => {
    const uuid = await command();

    const answer = await report({
      awaiting: [
        {
          command_uuid: uuid,
          message: "Bruno Gerente pede cancelar Café Expresso — já na fila da cozinha",
          requested_at: "2026-09-24T10:00:00+00:00",
        },
      ],
    });

    expect(answer).toEqual({ status: 200, accepted: [uuid] });
    const stored = await row(uuid);
    expect(stored.status).toBe("pending");
    expect(stored.awaiting_message).toContain("fila da cozinha");
    expect(stored.awaiting_confirmation_at?.toISOString()).toBe("2026-09-24T10:00:00.000Z");
  });

  it("repetir a espera não reescreve desde quando ela começou", async () => {
    const uuid = await command();
    await report({
      awaiting: [{ command_uuid: uuid, message: "na fila", requested_at: "2026-09-24T10:00:00Z" }],
    });

    await report({
      awaiting: [{ command_uuid: uuid, message: "pronto na cozinha", requested_at: "2026-09-24T11:00:00Z" }],
    });

    const stored = await row(uuid);
    expect(stored.awaiting_confirmation_at?.toISOString()).toBe("2026-09-24T10:00:00.000Z");
    expect(stored.awaiting_message).toBe("pronto na cozinha");
  });

  it("o resultado depois da espera fecha o comando, e a espera não volta", async () => {
    const uuid = await command();
    await report({ awaiting: [{ command_uuid: uuid, message: "espera" }] });

    await report({
      results: [{ command_uuid: uuid, status: "applied", message: "cancelado com aceite de Ana Caixa" }],
    });
    // Aviso atrasado, que chegou depois do resultado.
    const late = await report({ awaiting: [{ command_uuid: uuid, message: "espera de novo" }] });

    expect(late.accepted).toEqual([uuid]);
    const stored = await row(uuid);
    expect(stored.status).toBe("applied");
    expect(stored.result_message).toContain("Ana Caixa");
    expect(stored.awaiting_message).toBe("espera");
  });

  it("as duas listas no mesmo lote: o resultado prevalece", async () => {
    const uuid = await command();

    await report({
      awaiting: [{ command_uuid: uuid, message: "espera" }],
      results: [{ command_uuid: uuid, status: "refused", message: "Recusado no caixa" }],
    });

    expect((await row(uuid)).status).toBe("refused");
  });

  it("terminal antigo, sem o campo, relata como antes", async () => {
    const uuid = await command();

    const answer = await report({
      results: [{ command_uuid: uuid, status: "applied", message: "aplicado" }],
    });

    expect(answer).toEqual({ status: 200, accepted: [uuid] });
    expect((await row(uuid)).status).toBe("applied");
  });

  it("um terminal não marca espera em comando de outro", async () => {
    const foreign = await command(otherDevice);

    await report({ awaiting: [{ command_uuid: foreign, message: "não é meu" }] });

    expect((await row(foreign)).awaiting_confirmation_at).toBeNull();
  });

  it("data ilegível do caixa vira agora, e não derruba o lote", async () => {
    const waiting = await command();
    const done = await command();

    const answer = await report({
      awaiting: [{ command_uuid: waiting, message: "espera", requested_at: "ontem à noite" }],
      results: [{ command_uuid: done, status: "applied", message: "ok" }],
    });

    expect(answer.status).toBe(200);
    expect((await row(waiting)).awaiting_confirmation_at).not.toBeNull();
    expect((await row(done)).status).toBe("applied");
  });
});
