# Porte do PDV para C# (.NET 10)

Decisão de 25/09/2026: o PDV de balcão passa a ser escrito em C#, por etapas.
O motivo imediato é o **TEF**. As soluções de TEF do mercado (SiTef, PayGo,
SDKs de adquirente) são feitas para Windows e .NET, e é no .NET que as
bibliotecas e os exemplos de homologação existem.

Código em `apps/pdv-net/`. O PDV em Python (`apps/desktop-pdv/`) continua
vendendo até a paridade, e daqui em diante só recebe correções. **Toda fase nova
é em C#.**

**Interface em WinUI 3** (Windows App SDK 2.5), decidido em 25/09/2026, e o
trabalho segue na branch `port/csharp-winui`. O app é *unpackaged* e
*self-contained*: não usa MSIX, e o runtime do Windows App SDK vai dentro da
pasta. O mesmo instalador Inno copia a pasta e roda o `harden.ps1`, e a loja
sem internet não precisa baixar pré-requisito.

A arquitetura existe para que a tela seja testável:

| Projeto | Conteúdo | Testado por |
|---|---|---|
| `Pdv.Core` | regras puras: auditoria, TEF, dinheiro | xUnit |
| `Pdv.Data` | SQLite: venda, pagamento, diário do TEF, login | xUnit sobre o banco real |
| `Pdv.App` | *view models* (CommunityToolkit.Mvvm): o que cada tela faz | xUnit, sem janela |
| `Pdv.WinUI` | só XAML e *binding* | build no CI e abertura do binário |

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
- [x] **C3. Venda com TEF.** `Pdv.Data.Sales`.
  - **Ordem do fechamento.** Validar a quitação (antes de qualquer cartão),
    ler os cartões, gravar pedido, pagamentos e auditoria numa transação e
    confirmar.
  - **Desfazimento.** Cartão negado, cancelado ou com falha de comunicação,
    ou erro ao gravar, desfaz os cartões já aprovados daquela venda. Dá para
    pagar uma venda com dois cartões: a trava de pendência vale só para outra
    venda.
  - **Ligação com o TEF.** O `client_uuid` do pagamento no cartão é o
    `TransactionId` do TEF. É assim que a recuperação pergunta ao banco se a
    venda existe (`SaleRepository.WasRecorded`).
  - **Payloads.** `orders` sobe com as mesmas chaves do `push-day.json`, e
    `payments` com as mesmas chaves mais o `nsu`, que a nuvem já aceita e
    serve para conciliar com a adquirente.
  - **Regra a mais que o Python.** O troco nunca passa do que entrou em
    dinheiro. No Python, R$ 15 no cartão + R$ 5 em dinheiro numa venda de
    R$ 10 devolviam R$ 10 em espécie.
  - **Testes.** 13 de ponta a ponta sobre o banco real, incluindo queda antes
    e depois de gravar. As quatro regras foram verificadas por mutação.
- [x] **C3b. Item por unidade e baixa de estoque.**
  - **Baixa por ficha técnica** (`Pdv.Core.Stock`). Miligramas inteiros,
    `ROUND_HALF_UP` no fim e o rendimento dividindo. É conferida miligrama a
    miligrama contra `contracts/recipe-explosion.json`, gerado pelo
    `explode_recipe` do Python.
  - **Total do item por unidade.** Usa "meio para o par" (`ROUND_HALF_EVEN`),
    como o `quantize` do Python: 1,5 × R$ 3,33 = 499,5 centavos vira 500.
  - **`ItemRegistration`.** O item, o consumo por insumo, o movimento de
    estoque, o saldo e a auditoria entram numa transação só. O primeiro item
    abre o pedido, e os totais saem dos itens vivos no banco. Estoque
    negativo avisa e não trava a fila (padrão do Python), e há a opção de
    travar sem deixar nada gravado pela metade. `order_items` (com
    `ingredients`) e `stock_movements` sobem com as chaves do
    `push-day.json`.
  - **Venda completa.** Item mais cartão, de ponta a ponta. 121 testes.
- [ ] **C3c. Item por peso** (com o quadro cru da balança), **cancelamento de
      item e desconto** com autorização.
- [ ] **C4. Interface (WinUI 3).**
  - [x] **C4.0.** Casca WinUI 3 *unpackaged* e *self-contained*, que compila
        e abre.
  - [x] **C4a. Login por PIN** (`Pdv.Data.Auth`).
    - **Argon2id** no formato do `argon2-cffi`. Os hashes do Python
      (`contracts/pin-hashes.json`) verificam no C#, e o hash do C#
      verifica no Python (`crosscheck.py`, no CI).
    - **Política de PIN** com o mesmo veredito e a mesma mensagem do
      `validate_pin`, caso a caso.
    - **Freio.** Os mesmos números, na mesma `auth_throttle`, então um
      bloqueio feito pelo Python vale no C#. Há o piso monotônico contra o
      relógio do Windows atrasado, e o sucesso não zera o contador global.
    - **A mais que o Python.** Só aceita `argon2id` e recusa hash com
      parâmetros absurdos: um `m=4 GiB` plantado travaria o caixa.
    - **Bug pego pelo contrato.** PIN vazio (Enter sem digitar) derrubava o
      login com exceção, sem contar a tentativa.
    - 38 testes; as quatro regras do freio verificadas por mutação.
  - [x] **C4b. Tela de login.**
    - `LoginViewModel`: o teclado na tela só digita dígitos, até 12. O
      Argon2 roda fora da thread da tela. PIN errado limpa o campo e mantém o
      login. Um terminal não ativado avisa que é demonstração.
    - `TerminalProfile` lê a identidade das mesmas chaves de
      `device_settings` do Python, com a loja de demonstração quando o
      terminal não foi ativado.
    - A casca abre o banco e mostra o erro na janela, em vez de fechar
      calada.
    - `tools/ui-smoke.ps1` dirige o `PDV.exe` real pela UI Automation, no CI.
      Sobre um banco migrado pelo Python e com um PIN cadastrado pelo Python,
      confere que o caixa abre com o nome da loja, que o PIN errado mostra o
      motivo e que o PIN certo, digitado no teclado da tela, entra.
  - [x] **C4c. Tela de venda** (`SaleViewModel` e `CounterPage`).
    - O código de barras exato entra direto; o texto busca por nome ou
      código.
    - A tela mostra os itens e o total, e o troco conforme o operador digita
      o valor recebido.
    - Paga em dinheiro, débito, crédito ou PIX. Pagamento insuficiente diz
      quanto falta. Cartão negado mantém a venda para tentar outra forma.
    - A conversa do TEF ("Insira o cartão", "Transação aprovada") aparece na
      ordem em que chega.
  - [x] **C4d. Pendências do TEF na abertura.**
    - Ao entrar no caixa, a venda gravada é confirmada e a que se perdeu é
      desfeita.
    - O operador é avisado ("DESFEITA… retenha o comprovante"), e o primeiro
      cartão do dia passa.
  - **Prova no binário.** `tools/ui-smoke.ps1` faz login, bipa o código de
    barras e vende no débito com o TEF simulado, pela UI Automation, no
    `PDV.exe` real. Qualquer falha sai como anotação pública no GitHub, com a
    linha e o motivo, e o app grava o erro não tratado em
    `%LOCALAPPDATA%\ERPFood\PDV\pdv-winui.log`.
- [x] **C5a. Cofre de segredos** (`Pdv.Data.Secrets`, antecipado porque a
      venda precisa assinar a auditoria).
  - Mesmo arquivo `secrets/<nome>.bin`: DPAPI de máquina com a entropia
    `ERPFood.PDV.v1`, e o modo `PLAIN:` do Python lido.
  - Um segredo que existe e não decifra nunca é recriado: recriar faria a
    cadeia acusar adulteração.
  - DPAPI prende o blob à máquina, então a conferência é no CI e nos dois
    sentidos: o Python grava e o C# lê, o C# grava e o Python lê.
  - `Pdv.Data`, `Pdv.App` e os testes passam a `net10.0-windows`. 146 testes.
- [ ] **C5b. Sincronização e ativação.** Cliente HTTP da nuvem (push do
      outbox e pull do catálogo, com o mesmo contrato), ativação, heartbeat e
      comandos remotos.
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
