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
- [x] **C3c. Item por peso, cancelamento de item e desconto com autorização.**
  - **Balança** (`Pdv.Core.Scale`). Toledo Prix 3, Filizola e Urano, conferidas
    quadro a quadro contra `contracts/scale-weighing.json`, gerado pelo
    Python (`tests/test_scale_contract.py`). O contrato fixa as manias que
    divergiriam em silêncio:
    - o `strip()` do Python, que também tira 0x1C–0x1F;
    - o quadro cru gravado com `backslashreplace`;
    - os 5 dígitos finais na Toledo e os 5 iniciais na Filizola;
    - o último quadro completo do buffer, cortado em 4096 bytes.
  - **Leitura.** O peso só vale depois de 3 leituras estáveis idênticas, e o
    leitor roda fora da thread da tela. A `SerialScale` usa
    `System.IO.Ports`. Sem balança detectada em `device_settings`, entra a
    simulada, e a porta que não abre vira aviso no mostrador.
  - **Item pesado** (`RegisterWeighedItem`). Tara descontada, preço por peso
    `ROUND_HALF_UP` uma vez, e o quadro cru no item e na auditoria. Os eventos
    `weight_captured` e `item_registered` vão com as chaves do
    `push-day.json`. Diferente do Python, nada fica gravado quando a tara
    passa do peso (lá sobrava um pedido vazio).
  - **Cancelamento** (`SaleAdjustments.CancelItem`).
    - Só gerente, como no Python: o PIN do proprietário não libera.
    - O item recebe `canceled_at` e sobe como `update` com `client_uuid`
      novo.
    - O estoque volta por movimento de ajuste, e o evento vai como
      `critical`, com quem pediu e quem liberou.
  - **Desconto** (`ApplyDiscount`).
    - É percentual sobre o subtotal e substitui o anterior.
    - Arredonda "meio para o par", como o `quantize` do Python, e fica
      gravado em centavos.
  - **Regras a mais que o Python.**
    - O papel, o poder de autorizar e o teto de desconto são conferidos de
      novo dentro da transação, contra o cadastro de agora: um gerente
      desativado ou com teto reduzido depois do PIN não libera nada.
    - O desconto nunca passa do subtotal depois de um cancelamento; a NFC-e
      rateia o desconto pelos itens.
  - **Tela.** Mostrador da balança, F4 (cancelar o item marcado) e F6
    (desconto), com o diálogo de login e PIN. O diálogo confere com o freio,
    mostra o motivo da recusa e continua aberto. O `ui-smoke.ps1` pesa 847 g
    na balança simulada, dá 10% com o PIN do gerente e cancela o item, no
    `PDV.exe`.
  - 327 testes. Nove regras foram verificadas por mutação:
    - papel, `can_authorize` e teto;
    - sinal do estorno;
    - peso instável;
    - teto do desconto sobre o subtotal;
    - dígitos da Toledo;
    - estabilidade;
    - o freio no papel errado.
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
- [x] **C5b-1. Ativação** (`Pdv.Data.Provisioning`, `ActivationViewModel`,
      `ActivationPage`).
  - **A causa de "não consigo ativar".** O painel não tinha onde gerar o
    código: só os testes o inseriam no banco. Agora o dono ou o gerente gera
    em "Ativar um caixa" (`POST /api/panel/devices/activation-codes`). O
    código vale 15 minutos e é de uso único, e o banco guarda só o hash.
  - **Endereço e código** com as regras do Python, caso a caso contra
    `contracts/activation.json`. Exemplos: só ASCII no código (`Á` cai),
    HTTPS fora de localhost, porta e caminho preservados. O `Uri` do .NET
    não serve, porque reescreve o endereço.
  - **O que é gravado.** O token vai ao cofre *antes* de `device_settings`:
    um cofre que falha não deixa o terminal "ativado" sem credencial. A
    troca de loja com a fila cheia é bloqueada.
  - **Demonstração arquivada.** A ativação grava em `pdv_local.ativacao.db`,
    que é o schema do banco aberto, vazio. A próxima abertura arquiva a
    demonstração em `pdv_demo-AAAAMMDD-HHMMSS.db` e promove o banco novo. Os
    arquivos e a troca são os mesmos do Python, então um PDV termina o que o
    outro começou.
  - **Prova no binário.** O `ui-smoke.ps1` ativa o `PDV.exe` pela tela contra
    uma retaguarda simulada. O caixa reinicia sozinho e volta com a loja, e a
    demonstração fica arquivada. A volta do contrato vem pelo
    `crosscheck.py`: o Python abre o terminal que o C# ativou e lê o token no
    cofre. 197 testes, e duas regras verificadas por mutação.
- [x] **C5b-2. Sincronização** (`Pdv.Data.Sync`).
  - **Motor.** `OutboxReader`, `SyncEngine` e `HttpSyncTransport` seguem o
    Python regra por regra:
    - nada sai da fila sem o veredito da nuvem, e a resposta perdida depois
      do commit volta como `duplicate`;
    - a chave de idempotência sai do conteúdo;
    - status desconhecido é recusa;
    - rejeitado vai para a quarentena e nunca é apagado;
    - silêncio sobre um item não é sucesso;
    - backoff até 5 min, e quarentena depois de 25 tentativas.
  - **Pull de cadastro.** `users` e `products`, pelo mesmo `map_row`, caso a
    caso contra `contracts/sync.json`. O tenant é conferido de novo. Nulo
    mantém o valor local. Uma tabela que não aplica não mexe no cursor.
  - **Worker.** Cadência de 3/15/30 s e pull a cada 20 ciclos. O heartbeat
    sai mesmo quando o envio falha. Começa na abertura do caixa, antes do
    login, com conexão própria ao banco. A tela do caixa mostra a fila e a
    quarentena.
  - **Prova contra a retaguarda real.** `tools/CloudE2E` roda um turno com o
    código do PDV contra Next.js + Postgres: ativa, vende em dinheiro e
    débito, e os 10 itens voltam aplicados. A nuvem valida a cadeia de
    auditoria, o reenvio não duplica, e o heartbeat e o pull respondem. O
    `PDV.exe` aberto sobre esse banco manda o heartbeat sozinho.
  - 224 testes; duas regras verificadas por mutação.
- [x] **Serviço fiscal em C#** (`apps/fiscal-net`, DFe.NET): substituiu o
      serviço em Python. Ver `docs/fiscal_architecture.md`.
- [x] **C5b-3. Comandos remotos** (desconto e cancelamento pelo painel,
      assinados).
  - **Contrato.** `Pdv.Core.Remote.CommandProtocol` é conferido byte a byte
    contra `contracts/remote-commands.json`, gerado pelo Python
    (`tests/test_remote_contract.py`). O Python é conferido contra a nuvem
    (TypeScript) no teste de contrato que já existia, então C# = Python =
    nuvem. O contrato fixa:
    - o texto assinado: chaves por ponto de código, acento cru, `10` e
      `10.0` distintos, array na ordem e separador `\x1f`;
    - a janela de 12 h com 5 min de folga, a data sem fuso e a data
      ilegível contada como vencida;
    - o percentual lido como o `Decimal(str(valor))` e o `_plain` da
      mensagem.
  - **Fila e serviço** (`Pdv.Data.Remote`). `CommandInbox` usa a mesma tabela
    `remote_commands`. O status sai de `pending` na transação do efeito, e o
    relato é separado da decisão. `RemoteCommandService` tem as sete travas
    do Python:
    1. assinatura;
    2. janela;
    3. teto e papel lidos da réplica local;
    4. só pedido aberto;
    5. idempotência;
    6. chave local `remote.commands_enabled`;
    7. aceite presencial para o que a cozinha já tem, que não pode ser dado
       por garçom.
    Toda recusa vai ao ledger (assinatura, terminal e rede como
    `critical`), e o cancelamento usa o mesmo miolo do F4
    (`SaleAdjustments.CancelWithin`).
  - **Sincronização.** O ciclo busca, grava, aplica e só então relata
    (`SyncEngine.CommandCycleAsync`), em todo ciclo, depois do push. Só sai
    da fila de relato o que a nuvem nomear. Comando malformado ou de tipo
    desconhecido é descartado, não obedecido.
  - **Tela.** O pedido aberto relê itens e totais quando o painel o muda. Um
    aviso com "Ver pedidos" abre o diálogo de aceite ou recusa com login e
    PIN de quem está no caixa; a recusa exige motivo, que volta ao painel.
    O aceite usa a conexão da tela, e o ciclo, a dele.
  - 379 testes. As 15 travas foram verificadas por mutação, incluindo a
    trava final contra aplicação dupla (`status = 'pending'` no fechamento)
    e o relato que só marca o que a nuvem nomeou.
  - **Falta:** o ponta a ponta com o painel emitindo de verdade (exige
    sessão com senha e Turnstile). Entra no `tools/CloudE2E` na C7e.
- [x] **C6a. Sessão de caixa** (`Pdv.Data.Sales.CashSessionService`, mesma
      tabela `cash_sessions`).
  - **Abertura.** Entre o login e a venda pede o fundo de troco; em branco
    vale "sem fundo". Com a gaveta aberta por outro operador, não entra.
  - **Fechamento cego (F12).** A ordem é: a contagem, depois o PIN de quem
    libera, e só então esperado e divergência aparecem. O esperado é o fundo
    mais o dinheiro menos o troco, só de vendas pagas deste terminal desde a
    abertura. Declarado, esperado, auditoria (`warning` quando diverge) e
    outbox, com as chaves do `push-day.json`, entram numa transação só.
    Depois, volta ao login; a porta da balança é solta antes.
  - **Regras a mais que o Python.**
    - Quem autorizou é conferido de novo no fechamento.
    - Não fecha com venda na tela, que ficaria órfã na sessão seguinte.
  - 392 testes. Nove regras foram verificadas por mutação:
    - só dinheiro, só vendas pagas, desde a abertura e troco descontado;
    - gaveta de outro operador, na abertura e no serviço;
    - aviso de divergência;
    - autorizador reconferido;
    - venda na tela.

    O `ui-smoke.ps1` abre com R$ 100,00, vende e fecha às cegas com o PIN
    do gerente.
- [x] **C6b. Cashback, pré-pago, fiado e níveis de desconto**
      (`Pdv.Data.Customers`, mesmas tabelas; nenhum saldo é atualizado,
      todo saldo é soma de ledger).
  - **Cliente pelo telefone**, cadastrado na hora.
  - **Cashback.** Crédito `ROUND_HALF_UP` com teto e validade, idempotente
    por pedido e creditado na transação da venda. O resgate FIFO por lote
    existe como serviço e, como no Python, **não está ligado ao caixa**: a
    decisão desconto × pagamento vem antes (`docs/plan.md`).
  - **Pré-pago.** A carga pede o PIN de quem autoriza; o consumo é feito na
    transação da venda.
  - **Fiado.** Limite, vencimento, aging, cobrança na transação da venda e
    pagamento das cobranças mais antigas primeiro.
  - **Níveis.** "Funcionário" e "Dono" são protegidos: só proprietário
    atribui e o cliente não sai mais do nível. "Dono" sempre pede senha. O
    nível não acumula (prevalece o maior desconto), entra no pagamento e
    tem o papel conferido na transação.
  - **Regra a mais que o Python.** Pré-pago ou fiado sem cliente é recusado
    antes de qualquer cartão ser lido; lá, a recusa vinha com o cartão já
    aprovado.
  - **Tela.** "Cliente" (Ctrl+K), "Pré-pago" e "Fiado" no pagamento, F5
    fiado, F7 cashback, F11 carga e Ctrl+F6 níveis, como no Python. O
    `ui-smoke.ps1` identifica o cliente, faz a carga com PIN e vende no
    pré-pago.
  - 417 testes; 16 regras verificadas por mutação.
  - **Lacuna herdada do Python, não corrigida em silêncio.** O pagamento do
    fiado recebido no balcão não entra no esperado da gaveta: o fechamento
    cego acusa sobra do valor recebido em dinheiro. Anotado no `plan.md`.
- [x] **C6c. Impressora, cupom e DANFE NFC-e** (`Pdv.Core.Printing`,
      `Pdv.Data.Hardware.Printers`).
  - **Cupom e DANFE byte a byte.** São conferidos contra
    `contracts/receipts.json` e `contracts/danfe.json`, gerados pelo
    Python. As armadilhas fixadas:
    - PC850 com "?" no que não existe (o .NET faria "best fit", € → E);
    - um "?" por emoji (o .NET põe dois);
    - largura contada em pontos de código, como o `len()` do Python;
    - o valor nunca truncado;
    - a gaveta só com dinheiro;
    - a hora no fuso do caixa.
  - **DANFE.** As doze recusas do Python têm a mesma mensagem: chave de
    outro CNPJ, série trocada, "normal" sem protocolo, pagamento que não
    fecha, DV errado e outras.
  - **Entrega.** Spooler do Windows em RAW (`winspool.drv`) ou arquivo
    (`cupons/` ao lado do banco, na demonstração), pelas chaves
    `printer.*` de `device_settings`. A rota libusb do Python ficou de fora:
    ela troca o driver e tranca a impressora para outros programas.
  - **Fila de impressão.** Roda em thread própria, com três tentativas e
    aviso na tela se falhar: a venda já está gravada.
  - **Cupom montado do banco, não da tela.** Item cancelado fica fora e o
    desconto do painel entra. "Reimprimir" (Ctrl+P) é uma regra a mais que
    o Python.
  - 445 testes; seis regras verificadas por mutação. O `ui-smoke.ps1`
    confere o cupom da venda no débito gravado pela fila.
  - **Pendências.**
    - O DANFE só sai do papel quando o caixa pedir a NFC-e à nuvem (C6d).
    - CNPJ e endereço da loja vêm de `store.document` e `store.address`,
      que a ativação ainda não grava. Sem eles, o cupom mostra os
      marcadores do Python, que são fictícios. A retaguarda tem o CNPJ no
      cadastro fiscal e deve mandá-lo na ativação.
- [x] **C6d. NFC-e pedida pelo caixa (online)** (`Pdv.Core.Printing.NfceProcReader`,
      `Pdv.Data.Fiscal`, `Pdv.App.SaleDocuments`). Nem o Python fazia isto
      pela tela: `fiscal/cloud.py` só era usado em teste.
  - **Retaguarda.** As rotas do terminal (`/fiscal/issue` e `/fiscal/status`)
    devolvem `processed_xml`, só com a nota autorizada.
  - **O DANFE sai do XML autorizado, sem cálculo nenhum.** O leitor recusa:
    nota não autorizada, protocolo de outra chave, modelo ou emissão
    desconhecidos, `tPag` desconhecido, fração de centavo, itens − desconto
    diferente do `vNF`, DTD e XML acima de 1 MiB. Conferido contra
    `contracts/nfce-proc.json`, uma nota **gerada pelo motor do
    `fiscal-net`** (assinada, validada no XSD, com QR v3 e protocolo), cuja
    forma e cujos códigos `tPag` o `fiscal-net` exige no próprio teste. Em
    homologação o primeiro item sai com o texto que a SEFAZ exige, porque é
    o que está na nota.
  - **Cliente HTTP com a classificação do `cloud.py`.** Sem conexão (TCP,
    nome, TLS) é "offline"; timeout, 5xx, queda no meio e resposta ilegível
    são "resultado desconhecido", que daí em diante só consulta; 401/403 é
    autenticação; 4xx é recusa, com o `detail` da retaguarda.
  - **Um `request_uuid` por venda, gravado antes do primeiro envio**, em
    `fiscal_requests` (tabela do C#, `CREATE TABLE IF NOT EXISTS`, não
    sobe). Reinício, timeout e segundo plano pedem sempre com o mesmo uuid:
    a idempotência da retaguarda é que impede a segunda nota.
  - **Fluxo.** A venda fecha, a fila é empurrada (`SyncWorker.PushNowAsync`,
    serializado com o laço) e a nota é pedida com prazo curto. Autorizada →
    DANFE no lugar do cupom. Senão → cupom, e o segundo plano (conexão
    própria, prazo longo, espera crescente até 1 h) pede de novo o que não
    saiu e consulta o que ficou sem resposta. A reimpressão (Ctrl+P) da
    última venda sai como DANFE assim que a nota é autorizada. Total zero
    não pede nada, com ou sem rede.
  - **Desligado por padrão**: só com `fiscal.enabled = 1` em
    `device_settings` e terminal ativado, até a homologação na SEFAZ-RJ.
  - **Contingência offline não foi construída** (decisão pendente do dono:
    exige o A1 em cada caixa). Sem conexão, sai o cupom e a nota é pedida
    quando a rede voltar.
- [ ] **C6e. Servidor do salão** (`Pdv.Data.Edge`) — **em andamento**.
  - **Contrato por roteiro, não por função.** `contracts/salon.json` é
    gerado por `apps/desktop-pdv/tests/salon_script.py`: passos em JSON
    (mesa, comanda, item, cozinha, pareamento, sessão, gerente, turno,
    saldo de insumo), cada um com a resposta ou a recusa que o Python deu,
    os eventos que publicou no barramento e a fila do outbox inteira. O
    `SalonContractTests` roda os mesmos passos no C# e exige o mesmo texto.
    Ids viram `<idN>` na ordem de aparição e horário vira `<ts>`; hash do
    ledger, token, código de pareamento e segundos restantes saem trocados
    por marcador, porque dependem do relógio ou do acaso.
  - **Feito:** mesas (`TableService`), comandas e contas
    (`TableOrderService`: abrir idempotente pelo `client_uuid` do celular,
    lançar, pedir e desfazer conta, transferir, mover itens, juntar,
    receber inteiro e em parte, cancelar com gerente), cozinha
    (`KdsService`, com recall), barramento (`EventHub`, que nunca espera o
    assinante), pareamento com freio (`EdgeAuth`), sessão do garçom
    (`StaffSessions`), concessão de gerente em memória (`ManagerSessions`)
    e resultado do turno (`StaffReport`). 172 passos no roteiro. Nas
    comandas, na cozinha, nas credenciais e na baixa pelo salão, 42
    mutações, 40 mortas. As duas que sobrevivem são equivalentes: a
    observação vazia do ticket gravada como NULL (a tela recebe "" nos dois
    casos) e a gorjeta somada sem filtrar comanda paga (comanda aberta
    nunca tem gorjeta). As mensagens dos commits `d3081b4` e `00ce757` citam
    84 e 72 passos; os números certos são 69 e 70.
  - **Servidor HTTP** (`Pdv.Edge`: Kestrel dentro do PDV). As 28 rotas do
    `edge/server.py` e o WebSocket da cozinha, conferidos contra
    `contracts/salon-http.json`, gerado pelo FastAPI com o `TestClient`
    (`tests/salon_http_script.py`, 114 requisições): status, tipo do
    conteúdo, `X-Auth-Scope`, `Cache-Control` e corpo. O `SalonHttpContractTests`
    sobe o Kestrel de verdade em `127.0.0.1` e fala HTTP com ele.
    - **A ordem de conferência é a do FastAPI**: JSON ilegível (422) antes
      de tudo; depois aparelho (401), sessão (403 `staff`), gerente (403
      `manager`) e só então o corpo. Dois passos do roteiro existem só para
      prender essa ordem.
    - **422 de validação** sai como lista em `detail`, como no pydantic, e o
      contrato compara só o status: o app mostra "Erro 422" nos dois.
    - **Uma requisição por vez no banco** (trava por servidor), como o laço
      único do FastAPI serializava: uma conexão SQLite não é segura entre
      threads. O teste de 40 lançamentos simultâneos de 8 celulares prende
      isso.
    - **O app do garçom é o mesmo arquivo do Python**, embutido no
      executável por link no `.csproj` (sem cópia). Sai do `desktop-pdv`
      quando o Python for removido.
    - **Regra a mais que o Python:** `DELETE /orders/{id}/bill` numa comanda
      fechada respondia 500; corrigido nos dois (409), e o contrato exige
      que nenhuma rota responda 500.
    - 22 mutações na camada HTTP, todas mortas (duas depois de acrescentar
      os passos de ordem de conferência).
  - **TLS** (`SalonCertificate`, o `edge/tls.py`): autoassinado ECDSA P-256,
    398 dias, SAN com `localhost`, `127.0.0.1` e o IP da LAN, renovado 30
    dias antes e quando o IP muda. Mesmos arquivos PEM (`tls/edge-cert.pem`,
    `tls/edge-key.pem`) na mesma pasta: **cada PDV reaproveita o certificado
    do outro**, com a mesma digital — trocar obrigaria a loja a conferir a
    digital de novo em todo celular. O CI prova os dois sentidos
    (`crosscheck.py write-tls` → `SalonCertificateTests`, e a volta). No
    Windows a chave lida de PEM passa por PKCS#12: o SChannel recusa chave
    efêmera no servidor.
  - **No caixa** (`App.xaml.cs`): o salão sobe depois de o caixa abrir, uma
    vez por execução, com conexão própria ao banco. `PDV_EDGE=0` desliga e
    `PDV_EDGE_TLS=0` sobe em HTTP (com aviso no log), como no Python. O
    `ui-smoke.ps1` confere `/health` e o app do garçom em HTTPS na 8420.
  - **Anúncio na rede** (`ServiceAnnouncer`, o `edge/discovery.py`): mDNS
    `_pdvedge._tcp` com um responder próprio (sem pacote de terceiros),
    PTR, SRV, TXT e A com as propriedades e os TTLs do `zeroconf`, anúncio
    duas vezes na subida e despedida (TTL zero) na saída, consulta "legada"
    respondida direto a quem perguntou. O CI prova o formato pelo parser do
    próprio `zeroconf` (`crosscheck.py write-mdns` → resposta do C# → leitura
    pelo `DNSIncoming`) e o laço de rede com uma consulta UDP de verdade.
    Pacote malformado, com ponteiro em laço ou resposta de outro aparelho
    não vira pergunta. 9 mutações, 8 mortas; a que sobrevive (teto de
    saltos maior) é equivalente, o laço termina de todo jeito. Nome de loja
    com ponto vira um rótulo só (o Python o partiria em dois).
  - **O cancelamento pelo painel avisa a tela da cozinha** depois do commit,
    pelo mesmo barramento do salão (um só no app), como o Python. Antes o
    C# só marcava o ticket; a tela o tirava da fila apenas ao reconectar.
  - **Falta:** o painel do salão com a digital e o QR (C6f).
  - A mensagem do commit `444a7cb` diz 30 rotas; são 28 (27 HTTP e o
    WebSocket).
  - **Baixa de insumo pelo salão (correção nos dois PDVs, 26/09/2026).** O
    item lançado pelo garçom não baixava estoque: `add_item` gravava
    `consumptions=()`. Agora baixa **no lançamento**, pela mesma ficha do
    balcão, e o cancelamento da comanda estorna pelo consumo gravado em
    `order_item_ingredients` (o remoto já estornava daí). No C#, baixa,
    estorno e conferência de saldo viraram uma peça só (`StockWriter`),
    usada pelo balcão, pelo cancelamento e pela mesa. Sem saldo, com a loja
    bloqueando venda negativa, a mesa recusa como produto não vendável (o
    app do garçom já mostra essa recusa); o texto é o de cada PDV, que já
    diferia no balcão.
  - 48 testes novos no PDV, 3 na retaguarda e 1 no `fiscal-net`; 22 regras
    verificadas por mutação.
  - **Pendências.** Reimprimir o DANFE de uma venda que não é a última
    precisa de tela (C6f). O `ui-smoke.ps1` ainda não passa pelo fluxo
    fiscal, que exige retaguarda e serviço fiscal no ar.
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

## O que falta para não sobrar Python (25/09/2026)

**Já em C#.** Três partes fecham com testes, contratos e prova no binário ou
no contêiner:

- **PDV — fundação:** auditoria, venda com TEF (simulado), item por unidade
  e por peso (balança serial) com baixa por ficha técnica, cancelamento e
  desconto com autorização de gerente, login por PIN com freio, cofre,
  ativação, sincronização (push, pull e heartbeat) e comandos do painel.
- **PDV — tela:** WinUI de login, venda (com balança, F4 e F6) e ativação.
- **Serviço fiscal inteiro** (`apps/fiscal-net`). O serviço em Python foi
  removido.

**Ainda em Python: o PDV do balcão** (`apps/desktop-pdv`, ~16 mil linhas).
Na ordem em que dá para trocar:

| # | Fase | Python de hoje | O que é |
|---|---|---|---|
| ~~1~~ | ~~C3c~~ | ~~`hardware/scale/`, `services/pricing.py`, parte de `authorization.py`~~ | **Feito** (item por peso, cancelamento e desconto com autorização) |
| ~~2~~ | ~~C5b-3~~ | ~~`remote/`~~ | **Feito** (comandos do painel com as sete travas e o aceite no caixa) |
| ~~3~~ | ~~C6a~~ | ~~`services/cash_session.py`~~ | **Feito** (abertura com fundo de troco e fechamento cego) |
| ~~4~~ | ~~C6b~~ | ~~`cashback.py`, `prepaid.py`, `credit_account.py`, `discount_tiers.py`~~ | **Feito** (o resgate de cashback segue desligado até a decisão do contador) |
| ~~5~~ | ~~C6c~~ | ~~`hardware/printer/`, `fiscal/danfe.py`~~ | **Feito** (cupom e DANFE byte a byte; o DANFE vai ao papel com a C6d) |
| ~~6~~ | ~~C6d~~ | ~~`fiscal/cloud.py`~~ | **Feito** (NFC-e online pedida pelo caixa, DANFE do XML autorizado). A **contingência offline** (`fiscal/service.py`, `fiscal/gateway.py`: série própria, QR v3 assinado com o A1) espera a decisão do dono |
| 7 | C6e | `edge/` (~4.000) + `edge/webapp` | Servidor do salão para os celulares dos garçons, KDS, mesas, contas e descoberta na rede, em ASP.NET Core dentro do PDV. O app web do garçom é reaproveitado como está |
| 8 | C6f | `ui/salon_panel.py`, `ui/tables_dialog.py`, `ui/dialogs.py`, `ui/remote_dialog.py`, `services/staff_report.py` (~2.300) | Telas do salão, mesas, relatórios de equipe e diálogos que faltam no WinUI |
| 9 | C7a | `data/database.py` (~800), `data/seed.py` | Migrations em C#: criar e atualizar o banco sem o Python, e a demonstração |
| 10 | C7b | `provisioning/detection.py`, `selftest.py`, `smoke.py` (~1.000) | Detecção de periféricos, autoteste do pacote e fumaça pós-instalação |
| 11 | C7c | `setup_wizard.py`, `main.py`, `packaging/` | O instalador entrega o `PDV.exe` .NET self-contained, com reparo e atualização, e o `harden.ps1` continua |
| 12 | C7d | `apps/desktop-pdv/tests/test_*_contract.py`, `apps/pdv-net/crosscheck.py` | Os contratos gerados pelo Python viram arquivos congelados, e o crosscheck sai |
| 13 | C7e | `apps/cloud-api/scripts/e2e.py`, `e2e_terminal.py` | O ponta a ponta do CI passa a usar o `tools/CloudE2E` em C# contra a imagem Docker |
| 14 | — | `apps/desktop-pdv` inteiro e o job "PDV (Windows, Python)" do CI | Removidos quando 1 a 13 estiverem no ar e uma loja tiver rodado o PDV em C# |

**Fora do porte e dependente de terceiros:** TEF real (escolher o provedor) e
a homologação fiscal na SEFAZ-RJ (certificado A1 e inscrição da loja).

**Não é Python e não entra no "tudo C#"** sem uma decisão à parte: a
retaguarda e o painel web são TypeScript (Next.js), no Coolify, e o app do
garçom é web.

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
