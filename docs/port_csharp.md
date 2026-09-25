# Porte do PDV para C# (.NET 10)

Decisão de 25/09/2026: o PDV de balcão passa a ser escrito em C#, por etapas.
O motivo imediato é o **TEF**. As soluções de TEF do mercado (SiTef, PayGo,
SDKs de adquirente) são feitas para Windows e .NET, e é no .NET que as
bibliotecas e os exemplos de homologação existem.

Código em `apps/pdv-net/`. O PDV em Python (`apps/desktop-pdv/`) continua
vendendo até a paridade, e daqui em diante só recebe correções. **Toda fase nova
é em C#.**

## As regras da transição

1. **Mesmo banco.** O C# abre o mesmo `C:\ProgramData\ERPFood\PDV\pdv_local.db`,
   com o mesmo schema. Tabela nova do C# entra com `CREATE TABLE IF NOT EXISTS`,
   sem mexer no `user_version`, até o C# assumir as migrations (fase C7).
2. **Mesma nuvem.** Mesmas rotas, mesmos payloads. O contrato
   `contracts/push-day.json` vale para os dois.
3. **Contratos gerados pelo Python, exigidos pelo C#.** O que os dois precisam
   gravar igual (hash da auditoria, hash de PIN, formato do cofre de segredos,
   payload de sync) vira um arquivo em `contracts/` gerado pela implementação
   Python. O teste C# exige igualdade byte a byte. Mudou de um lado, o outro
   reprova.
4. **Mesma instalação.** Mesmo instalador (Inno Setup), mesmo `harden.ps1`,
   mesma pasta de dados. O instalador troca o binário; os dados ficam.
5. **Teste pulado reprova**, como no Python. Cada regra de segurança é
   verificada por mutação: remove-se a regra, o teste tem de falhar.

## Fases

- [x] **C1. Fundação, auditoria e núcleo do TEF.**
  - Solução .NET 10 com aviso como erro, e um job no CI (Windows).
  - Cadeia de auditoria compatível: JSON canônico (incluindo o `repr` de
    float do Python), HMAC-SHA256, genesis e verificação. 18 vetores de
    `contracts/audit-chain.json`, e uma cadeia gravada pelo Python que o C#
    verifica, com edição, remoção e segredo errado detectados.
  - TEF independente de provedor (`Pdv.Core.Tef`):
    - o diário registra a transação **antes** de o cartão ser lido;
    - a aprovação fica pendente até a venda ser gravada; venda gravada →
      confirmar, venda perdida → desfazer;
    - a abertura do caixa resolve as pendências antes de qualquer venda;
    - um cartão novo é bloqueado enquanto houver pendência.
  - O simulador de TEF inclui o lado da adquirente, para os testes
    responderem onde terminou o dinheiro do cliente.
  - 31 testes, e as quatro regras de segurança verificadas por mutação.
- [x] **C2. Dados (base).** `Pdv.Data`, com `Microsoft.Data.Sqlite` sobre o
      mesmo banco.
  - **Schema.** `contracts/pdv-schema.sql` é gerado do `migrate()` do Python
    (`tests/test_schema_contract.py`). Os testes do C# criam os bancos a
    partir dele, e os triggers de imutabilidade do ledger vieram junto.
  - **Abertura.** O C# só abre a versão 14 (a versão 0, uma anterior ou uma
    mais nova são recusadas, dizendo o que fazer) e não cria banco que não
    existe. Usa os mesmos PRAGMAs do Python (WAL, `synchronous=FULL`).
  - **Auditoria.** A cadeia é gravada na transação da operação e o elo vai
    ao outbox no formato do Python (`", "` e `": "`). O rollback não deixa
    rastro nem fila. O CI verifica, com o `AuditService` do Python, um ledger
    gravado pelo C# (`crosscheck.py`).
  - **Diário do TEF.** `SqliteTefJournal` usa conexão própria, então não volta
    atrás junto com a venda. A tabela é `tef_transactions`, e a pendência
    sobrevive à queda.
  - **Ordem da venda com cartão.** Autorizar, depois abrir a transação e
    gravar, depois confirmar. No WAL só um escreve por vez.
  - 43 testes.
- [ ] **C2b. Repositórios da venda.** Pedido, item e pagamento; o payload do
      outbox igual ao `push-day.json`.
- [ ] **C3. Venda com TEF.** Fechamento de venda:
      - autorizar → gravar venda e pagamento (NSU e `transaction_id`) na mesma
        transação → confirmar;
      - erro ao gravar → desfazer;
      - recuperação na abertura, com a mensagem ao operador;
      - teste de ponta a ponta com o simulador, incluindo queda entre as
        etapas.
- [ ] **C4. Interface (WPF).** Login por PIN, com Argon2id compatível com os
      hashes do Python (contrato). Também a tela de venda, o pagamento com a
      conversa do TEF e o diálogo de pendências na abertura.
- [ ] **C5. Sincronização e ativação.** Cliente HTTP da nuvem, cofre de
      segredos (DPAPI, mesmo formato: contrato), heartbeat e comandos
      remotos.
- [ ] **C6. O resto da paridade.**
      - Periféricos: balança serial e impressora ESC/POS.
      - NFC-e.
      - Servidor do salão (ASP.NET Core servindo o mesmo app do garçom).
      - Sessão de caixa, cashback, pré-pago, conta assinada e KDS.
- [ ] **C7. Troca.**
      - O instalador passa a entregar o `PDV.exe` .NET (self-contained), com
        autoteste dentro do pacote.
      - O C# assume as migrations a partir do schema 15.
      - O Python é aposentado.
- [ ] **TEF real.** Quando o provedor for escolhido, entra uma implementação
      de `ITefProvider`: CliSiTef por P/Invoke, PayGo ou TEF Dial por
      arquivos. Mais o roteiro de homologação do provedor, rodado contra o
      caixa.

## TEF: o que já está decidido

O ciclo de vida da transação mora no `TefCoordinator` e vale para qualquer
provedor. O provedor só implementa quatro operações: autorizar, confirmar,
desfazer (idempotente, aceitando transação que nunca chegou ao host) e o nome.

Os centavos do valor escolhem o roteiro do simulador:

| Centavos | Roteiro |
|---|---|
| `,51` | negada ("saldo insuficiente") |
| `,52` | o cliente cancela no pinpad |
| `,53` | a adquirente aprova e a resposta se perde (testa o desfazimento) |
| outro | aprovada |
