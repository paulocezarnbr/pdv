/**
 * A forma de um comando na resposta da API.
 *
 * Fica aqui, e não dentro de uma das rotas, porque **três** rotas devolvem a
 * mesma estrutura: `issue` (o painel emite), `pending` (o terminal busca) e,
 * mais adiante, qualquer tela de histórico. Uma rota importando outra acopla
 * dois contratos HTTP que precisam poder mudar em separado — e o Next ainda
 * carregaria o módulo inteiro da outra rota, com as dependências dela, para
 * usar uma função de trinta linhas.
 *
 * O formato é lido pelo terminal (`pdv/remote/protocol.py`), então mudar nome
 * de campo aqui quebra os PDVs já instalados. Campo novo é aditivo; campo
 * renomeado exige atualizar os dois lados.
 */

export interface CommandRow {
  command_uuid: string;
  tenant_id: string;
  store_id: string;
  device_id: string;
  kind: string;
  payload_json: string;
  issued_by_user_id: string;
  issued_by_name: string | null;
  issued_at: Date | string;
  signature: string;
}

export interface CommandOut {
  command_uuid: string;
  tenant_id: string;
  store_id: string;
  device_id: string;
  kind: string;
  payload: Record<string, unknown>;
  issued_by_user_id: string;
  issued_by_name: string;
  issued_at: string;
  signature: string;
}

export function toCommandOut(row: CommandRow): CommandOut {
  return {
    command_uuid: row.command_uuid,
    tenant_id: row.tenant_id,
    store_id: row.store_id,
    device_id: row.device_id,
    kind: row.kind,
    // O `payload_json` é guardado no formato canônico — o mesmo texto que
    // entrou no HMAC. Reserializar aqui e mandar o objeto é seguro porque o
    // terminal recanoniza antes de conferir a assinatura; mandar o texto cru
    // obrigaria o app a fazer o parse duas vezes.
    payload: JSON.parse(row.payload_json) as Record<string, unknown>,
    issued_by_user_id: row.issued_by_user_id,
    issued_by_name: row.issued_by_name ?? "",
    issued_at:
      row.issued_at instanceof Date ? row.issued_at.toISOString() : row.issued_at,
    signature: row.signature,
  };
}
