# plan.md — ERP SaaS Food Service (Híbrido Offline-First)

> **Contrato de contexto.** Este arquivo é a fonte da verdade sobre *o que* será
> construído e em *qual ordem*. Nenhuma feature entra no código sem estar aqui.
> Mudou o escopo? Atualize este arquivo **antes** de escrever código.
> Companheiros obrigatórios: [`tech_stack.md`](./tech_stack.md) (o *como*) e
> [`der.md`](./der.md) (o *dado*).

---

## 1. Visão do Produto

ERP SaaS multi-tenant para restaurantes, lanchonetes e confeitarias, composto por
três clientes sobre um único backend:

| Cliente | Papel | Conectividade |
|---|---|---|
| **Cloud Backend + Web App** | Retaguarda, BI, cadastros, KDS, cardápio QR, WhatsApp IA | Online |
| **Desktop PDV (Python)** | Caixa de balcão, periféricos físicos, servidor local de contingência | **Offline-first** |
| **Mobile Garçom (RN/Flutter)** | Mesas, comandas, lançamento de pedidos | Híbrido (LAN ↔ Cloud) |

**Princípio arquitetural nº 1 — o restaurante nunca para.** Queda de internet
degrada funcionalidades acessórias (BI, WhatsApp, cardápio QR), **nunca** a
operação de venda. O Desktop PDV é autossuficiente e atua como *edge server* da loja.

**Princípio nº 2 — modularidade por licença.** Todo módulo é ligável/desligável
por tenant via feature flag persistida (`tenant_modules`), sem rebuild e sem deploy.

**Princípio nº 3 — dinheiro exige prova.** Toda operação que reduz receita
(cancelamento, desconto, sangria, fiado) gera registro imutável encadeado por
hash, com identificação do autor e do autorizador.

---

## 2. Módulos do Ecossistema

Cada módulo é um *bounded context*: schema próprio (ou prefixo de tabelas),
serviço próprio no backend e, quando aplicável, pacote próprio no desktop/mobile.

| # | Módulo | Núcleo (MVP) | Clientes afetados |
|---|---|---|---|
| M01 | **Core / Tenancy** | Tenants, lojas, usuários, RBAC, licenças de módulo | Todos |
| M02 | **Catálogo** | Produtos, categorias, variações, combos, produto pesável | Todos |
| M03 | **Estoque & Ficha Técnica** | Insumos, receitas, baixa fracionada, inventário, perdas | Cloud + Desktop |
| M04 | **PDV Balcão** | Venda, pagamentos, sangria/suprimento, periféricos | Desktop |
| M05 | **Sincronização** | Outbox, idempotência, LWW + regras de merge, replay | Desktop + Mobile + Cloud |
| M06 | **Salão / Comandas** | Mesas, comandas, transferência, divisão de conta | Mobile + Desktop |
| M07 | **KDS** | Fila de preparo, tempos, bump/recall, alertas | Cloud (WS) + Desktop (LAN) |
| M08 | **Financeiro** | Cashback, crédito pré-pago, fiado, níveis de desconto | Cloud + Desktop |
| M09 | **Segurança / Anti-Furto** | Conciliação cega, ledger de auditoria, senha de gerente | Todos |
| M10 | **Fiscal** | NFC-e / SAT CF-e, contingência offline, cancelamento | Desktop + Cloud |
| M11 | **Cardápio QR + IA** | Menu digital, upsell contextual, pedido na mesa | Cloud |
| M12 | **WhatsApp IA** | Cloud API oficial, LLM anotador de pedidos | Cloud |
| M13 | **IA Previsão de Demanda** | Forecast de insumos, sugestão de compra | Cloud |
| M14 | **BI / Relatórios** | DRE simplificado, CMV, curva ABC, ranking | Cloud |
| M15 | **Painel Remoto** | Telemetria ao vivo das lojas + comandos à distância (alterar pedido, conceder desconto, cancelar item) | Cloud + Desktop |

---

## 3. Roadmap por Fases

Cada fase termina com **critério de aceite verificável**. Não avance sem ele.

### Fase 0 — Fundação (Sprint 1–2)
- [ ] Monorepo, CI, lint/format/type-check nas três linguagens.
- [ ] Modelo de dados M01 + RLS no PostgreSQL (`tenant_id` obrigatório).
- [ ] Auth (JWT + refresh) com claims `tenant_id` e `store_id`.
- [ ] Schema SQLite espelho do subset offline (ver `der.md`).
- **Aceite:** dois tenants distintos não enxergam dados um do outro nem com token
  adulterado — teste automatizado de vazamento por RLS.

### Fase 1 — PDV Balcão Offline (Sprint 3–5) ✅ **CONCLUÍDA**
- [x] App PySide6 com SQLite local em WAL e migrations versionadas.
- [x] Driver de balança serial (Toledo Prix 3 / Filizola / Urano) com leitura
      contínua em thread separada e detecção de peso estável.
- [x] Cálculo de preço por peso com arredondamento fiscal (ROUND_HALF_UP, 2 casas).
- [x] Baixa fracionada de insumos por ficha técnica, em **miligramas inteiros**.
- [x] Ledger de auditoria encadeado por **HMAC-SHA256** com segredo por
      dispositivo (SHA-256 puro seria forjável por quem tem o código).
- [x] Impressão ESC/POS bruta 80 mm (Epson TM-T20X) + guilhotina + gaveta.
- **Aceite:** venda pesada completa com o cabo de rede desconectado; reinício do
  app preserva a venda; cupom sai com corte e a gaveta abre.

### Fase 2 — Sincronização (Sprint 6–7) ✅ **CONCLUÍDA**
- [x] Padrão **Outbox** no SQLite: nada sobe fora dele.
- [x] `client_uuid` (UUIDv7) gerado no cliente = chave de idempotência no servidor.
- [x] Upload em lote atômico: servidor aplica em transação única e responde
      `applied` / `duplicate` por item; cliente só marca `is_synced` com ACK.
- [x] Download incremental por `updated_at` + cursor por tabela.
- [x] Resolução de conflito: vendas são *append-only* (nunca conflitam);
      cadastros usam LWW por `updated_at` com desempate por `server_seq`.
- **Aceite:** 500 vendas offline com 3 quedas de rede no meio do upload →
  zero duplicidade e zero perda no PostgreSQL.
- [x] **Contrato terminal ↔ nuvem, provado ponta a ponta.** O aceite acima
      rodava contra uma nuvem dublada que aceitava qualquer coisa, e a nuvem
      era testada com payloads escritos à mão. Nenhum terminal real
      sincronizava, por quatro motivos independentes, todos corrigidos:
      1. o fechamento do pedido saía como INSERT sem `local_number` e a nuvem
         só sabia inserir — o `update` virou UPDATE de verdade, com colunas e
         dono restritos (`UPDATABLE`) e idempotência própria
         (`sync_applied_updates`);
      2. a ativação não entregava o segredo do ledger, e a nuvem respondia
         409 a todo lote;
      3. a sincronização e a ativação colavam a rota na origem, sem `/api`;
      4. o `PDV.exe` ignorava o provisionamento: abria `./pdv_local.db` com os
         IDs de demonstração e a chave de desenvolvimento (`pdv/runtime.py`).
      Um item que o banco recusa vai sozinho para a quarentena, em vez de
      derrubar o lote e parar a fila. O cancelamento de item do balcão e o
      mapa do salão passaram a subir. `scripts/e2e_terminal.py` roda o código
      do PDV contra a imagem Docker na CI; os quatro defeitos foram
      reintroduzidos um a um e todos derrubam o teste.

### Fase 2.5 — Instalador Único e Autossuficiente (Sprint 7–8)

**Requisito do cliente:** o instalador é **um único `.exe`**. O lojista executa,
clica em avançar e o PDV fica operante. Nenhuma etapa manual, nenhum pré-requisito
a baixar à parte, nenhum prompt de linha de comando.

- [x] `.exe` único gerado por Inno Setup, com elevação obrigatória.
- [x] **Runtime Python e todas as bibliotecas embarcados** no pacote
      (PyInstaller/Nuitka). O cliente **não** instala Python nem roda `pip`.
- [x] Endurecimento de ACL aplicado automaticamente durante a instalação.
- [x] **Visual C++ Redistributable embarcado** e instalado silenciosamente se
      ausente. Sem ele o Qt não carrega e o app morre sem mensagem — é a causa
      número um de "instalei e não abre" em máquina recém-formatada.
- [ ] **Driver USB-serial da balança** (CH340 / FTDI / Prolific) detectado e
      instalado sob demanda. Cabo de balança raramente traz driver nativo.
- [ ] **Driver da impressora Epson TM-T20X** instalado silenciosamente, com a
      fila criada e nomeada conforme esperado pelo `PrinterConfig`.
- [x] **Detecção automática de periféricos** no fim da instalação: varrer portas
      COM, identificar a balança pelo protocolo e localizar a impressora,
      gravando o resultado em `device_settings`. O lojista não sabe o que é
      uma porta COM e não deveria precisar saber.
      *Regra aprendida em campo:* a classificação da impressora casa **modelo**
      (`TM-T`, `MP-4200`, `DR700`), nunca marca. Testar por "epson" classificou
      uma multifuncional a jato de tinta L8180 como térmica — em produção o PDV
      teria mandado ESC/POS cru para ela. Sem térmica reconhecida a resposta é
      `None` e o cupom vai para arquivo: a impressora padrão do Windows **não**
      é promovida a candidata.
- [x] `device_secret` gerado localmente na primeira execução e protegido por
      DPAPI em escopo de máquina. Nunca entra no banco que ele protege, e
      trafega **uma única vez**, na ativação, por HTTPS: a nuvem confere cada
      elo do ledger com a mesma chave (HMAC é simétrico), guarda a cópia só
      para conferir e não a troca numa reativação.
- [x] **Ativação do terminal**: o lojista digita um código curto gerado no
      painel (`/ACTIVATIONCODE=` para implantação em massa) e o terminal recebe
      `tenant_id`, `store_id`, `device_id` e o token de sincronização, guardado
      no DPAPI e nunca em `device_settings`. Código de uso único, validade de
      15 min, consumido atomicamente e com teto de tentativas por IP.
      *Regra que protege o faturamento:* um terminal com fila pendente **não**
      troca de tenant. Aquelas vendas foram registradas sob o CNPJ antigo;
      reapontar antes de esvaziar a fila mandaria o faturamento de uma loja
      para outra — erro que só aparece na conciliação fiscal do mês.
      Ativar não é obrigatório: sem código o PDV instala, vende e acumula na
      fila. Travar a instalação por falta de internet transformaria um
      contratempo em visita técnica perdida.
- [x] **Teste de fumaça pós-instalação**: verifica gravação no banco e modo WAL,
      integridade da cadeia de auditoria, resposta da balança, impressão de um
      cupom de teste (com acentuação, corte e pulso de gaveta) e presença de
      catálogo. Cada verificação é independente — uma balança desligada não
      esconde que a impressora também está sem papel. Relatório na tela e em
      `ProgramData\ERPFood\PDV\logs\setup.log`; saída 0/2/3 para o instalador.
- [x] Atalho "Reconfigurar periféricos" no menu Iniciar (`PDVSetup.exe
      --detect-only`): troca de balança ou de porta USB se resolve sem
      reinstalar nada.
- [x] Atualização in-place preservando o banco e a fila de sincronização.
      Dados vivem em `ProgramData`, fora de `{app}`: a atualização troca binário
      e nada mais. O PDV aberto é encerrado antes (mantém DLLs travadas), há
      bloqueio de downgrade (binário antigo não deve encontrar migrations que
      desconhece) e aviso para fechar o caixa.
      *Regressão evitada:* a redetecção de periféricos **não** sobrescreve a
      configuração existente numa atualização. Uma balança desligada no instante
      do update rebaixaria o terminal para `simulated` e a loja pararia de vender
      por peso sem nada ter quebrado de fato. Sobrescrever é ato deliberado, via
      atalho "Reconfigurar periféricos".
- [ ] Assinatura digital do instalador e dos binários (sem ela, o SmartScreen
      barra e parte dos lojistas desiste da instalação).
      **Bloqueado por insumo, não por código:** o `build.ps1` já assina os dois
      executáveis e o instalador, com carimbo de tempo, assim que receber
      `-SignCert`. Falta o certificado de Assinatura de Código (EV, emitido para
      a pessoa jurídica) — nada no repositório destrava isso.
- [x] Desinstalador que **preserva** os dados da loja.

**Aceite:** numa máquina Windows recém-formatada, sem Python, sem Visual C++ e
sem drivers, um único duplo-clique deixa o PDV vendendo — com balança lendo,
impressora cortando e a primeira venda aparecendo na nuvem. Zero intervenção
técnica.

### Fase 3 — Mobile Garçom + KDS (Sprint 8–10)

> **O terminal vira servidor.** Enquanto a internet estiver fora, é este processo
> que numera pedidos, guarda a comanda e alimenta a cozinha. O celular do garçom
> é cliente dele, não da nuvem.

- [x] Descoberta do PDV na LAN via mDNS (`_pdvedge._tcp`).
      Falhar no anúncio **não** impede o PDV de vender, e o app mantém a opção
      de endereço manual: roteador com isolamento de cliente bloqueia mDNS, e
      esconder a saída manual transformaria um contratempo em chamado.
- [x] Desktop expõe API local (FastAPI embarcado) + WebSocket para o KDS.
      Escuta em `0.0.0.0` porque precisa aceitar os celulares, mas **nenhuma**
      rota de negócio confia no IP de origem: a LAN da loja é a mesma rede do
      Wi-Fi do cliente.
- [x] **Pareamento presencial**: o código é gerado na tela do caixa, vale 5 min
      e serve uma vez só. O código agora tem **8 dígitos**, contador visível e
      revogação antecipada no caixa. O acesso físico ao balcão é a âncora — quem não chega
      lá não pareia, mesmo estando na rede. Só o hash do token é gravado, e a
      revogação vale no instante seguinte.
- [x] **TLS na rede local**: o PDV gera e renova um certificado ECDSA para o
      hostname e os IPs locais. O fingerprint aparece no caixa para conferência
      presencial; PIN, token e pedidos não atravessam o Wi-Fi em texto claro.
- [x] **Identidade fixa do garçom sobre o aparelho pareado**: parear identifica
      o dispositivo; login identifica a pessoa do turno. O mesmo Argon2id,
      política de PIN e rate limit persistente do caixa protegem `joao`,
      `maria` e os demais usuários. Trocar de funcionário não exige novo
      pareamento, e revogar o aparelho continua encerrando seu acesso.
- [x] KDS com tempo por item, bump/recall e alerta de atraso.
      *Recall* existe porque a cozinha erra: bateu pronto no prato errado e
      precisa desfazer sem cancelar o item — cancelar mexe na venda, o que é
      decisão de gerente, não de quem está na chapa. O tempo conta do
      `queued_at`: o que importa é há quanto tempo o cliente pediu.
- [x] **Item por peso é recusado no celular.** Quem pesa é a balança do balcão,
      que guarda o quadro cru como prova pericial. Aceitar peso digitado por
      quem cobra abriria exatamente o buraco que o M09 existe para fechar.
- [ ] Mobile decide rota: LAN se disponível, senão Cloud; fila local se ambos
      caírem. **O app em si (React Native/Flutter) ainda não foi escrito** — o
      contrato do lado do servidor está pronto e testado.
- **Aceite:** ✅ pedido lançado no celular aparece no KDS em **445 ms**, medido
  sobre HTTP + WebSocket reais contra o servidor rodando (critério: < 1 s).

**O invariante que sustenta a fase:** o `client_uuid` gerado no celular é
preservado ponta a ponta. O app tem duas rotas até a nuvem — a LAN, por este
terminal, e a internet, direto — e escolhe uma sem poder confirmar que a outra
não entregou. Se o terminal gerasse uuid próprio ao receber o pedido, a mesma
comanda chegaria à nuvem com dois identificadores e o restaurante seria cobrado
duas vezes. Preservar o uuid da origem é o que faz os dois caminhos convergirem.

### Fase 3.5 — Painel Administrativo Remoto (Sprint 10–12)

**Requisito do cliente:** acompanhar as estatísticas atualizadas de qualquer
lugar pelo navegador e **agir à distância** — alterar um pedido, conceder um
desconto, cancelar um item — sem precisar estar na loja.

#### 3.5.a — Telemetria ao vivo (caminho de leitura)
- [~] Dashboard web por loja: faturamento do dia, ticket médio, vendas/hora,
      ranking de produtos, CMV, margem, descontos e gorjetas. Atualiza a cada
      30 s e sob demanda; WebSocket fica para o próximo incremento.
- [x] Visão consolidada multi-loja para o dono (respeitando `tenant_id`) e
      filtro de loja validado no servidor, não confiado ao navegador.
- [~] Estado operacional de cada terminal: online/offline, **itens pendentes de
      sincronização**, último ACK, drift de relógio, papel da impressora.
      A cada ciclo — **inclusive quando o envio falhou**, que é quando importa —
      o terminal relata fila viva, quarentena com o último motivo, a idade do
      item mais antigo e o próprio relógio (`POST /api/devices/heartbeat`). O
      desvio é medido pela nuvem, não informado pelo caixa. O painel mostra, na
      linha do terminal e em ordem de gravidade, "N vendas presas há X min"
      (fila parada há mais de 15 min), a quarentena com o motivo e "relógio do
      caixa -9 min" (desvio acima de 2 min). Foi o que faltou para enxergar o
      defeito de sincronização: o terminal aparecia "online" com o faturamento
      vazio. **Falta** o papel da impressora (exige consulta de status ESC/POS).
- [x] Indicador explícito de **frescor do dado**: "atualizado há 8 s" vs
      "terminal offline há 40 min — números podem estar defasados". Dashboard
      que mente sobre estar atualizado é pior que dashboard sem dado.
- [~] Alertas: caixa mudo, divergência de conciliação, pico de cancelamentos,
      estoque negativo, cadeia de auditoria acusando adulteração.
      Alertas de fraude/ledger em aberto já aparecem; as demais regras entram
      junto da conciliação cega da Fase 4.

#### 3.5.b — Comandos remotos (caminho de escrita)
> ⚠️ **Inverte o modelo de confiança.** Até aqui o PDV só *enviava*. Aceitar
> comandos de fora transforma o terminal em alvo: quem comprometer o painel
> passa a conceder descontos e cancelar itens em todas as lojas. Por isso o
> caminho de escrita tem exigências que o de leitura não tem.

- [x] **Padrão Inbox** (espelho do Outbox): o comando entra numa fila no
      terminal e é aplicado **uma única vez**, com `command_uuid` idempotente.
      Reenvio por timeout não concede o desconto duas vezes.
- [x] **Offline-first também na ida:** terminal sem internet recebe o comando
      ao reconectar. O painel mostra `pendente` → `entregue` → `aplicado` ou
      `recusado`, nunca um "sucesso" otimista.
- [x] **Mesmos tetos do presencial.** Desconto remoto respeita
      `discount_tiers.max_discount_cents` e o limite do perfil de quem emitiu.
      Estar longe não amplia poder — se não pode no balcão, não pode remoto.
- [x] **Escopo restrito:** só pedido **em aberto**. Venda fechada altera-se por
      estorno/nova venda; documento fiscal transmitido, só por cancelamento
      fiscal. O painel nunca reescreve o passado.
- [x] **Auditoria com dupla identidade:** cada evento grava o `actor` remoto, o
      `device_id` alvo, IP e canal (`remote_panel`). Relatório de cancelamentos
      separa presencial de remoto — senão o painel vira a rota limpa para o
      mesmo furto que o M09 combate.
- [ ] **Confirmação no terminal para operações de risco:** cancelar item já
      impresso ou abrir gaveta exige aceite do operador presente. Abrir gaveta
      remotamente sem ninguém por perto é convite a furto.
- [~] **Autenticação forte do emissor:** assinatura HMAC-SHA256 do comando
      conferida no terminal contra o `device_secret`, com janela de validade de
      12 h e tolerância de 5 min de drift — o PDV não obedece a quem não prova
      quem é. **Falta** o 2FA no lado da nuvem, que é onde ele mora.
- [x] **Rate limit e kill switch.** Duas travas, em lados opostos e de
      propósito: a chave local (`remote.commands_enabled`, ausente = ligado)
      não depende de a nuvem cooperar — que é justamente o que não se pode
      supor quando o painel é o que foi comprometido; e o teto por operador na
      nuvem (60 em 10 min) limita o estrago de uma credencial vazada ao que dá
      para reverter numa manhã, em vez de uma noite inteira de descontos em
      todas as lojas.
- [x] **Transporte.** `fetch_commands` / `report_commands` no `sync/`, o
      roteador `commands/` na API e a fiação no `main.py`. Três decisões que
      ficam registradas:

      * **A entrega não consome.** A nuvem reentrega até o terminal relatar.
        Consumir na entrega perderia o comando de vez se o terminal morresse
        entre receber e gravar — e perder em silêncio é pior que entregar duas
        vezes, porque a segunda colide no `command_uuid` e vira no-op.
      * **Aplicar vem antes de relatar, sempre.** Relatar primeiro deixaria o
        painel dizendo "aplicado" para um desconto que o terminal ainda pode
        recusar, e o gerente iria embora confiando no que leu.
      * **Só sai da fila de relato o que a nuvem nomear.** Um "ok" genérico
        esconderia gravação parcial, e o painel mostraria `pendente` num
        comando já aplicado — que é o estado em que alguém reemite o desconto
        na mão.

      O canal é **opcional nos dois sentidos**: nuvem antiga que não fala de
      comando continua servindo terminal novo, e terminal montado sem o serviço
      só envia. Em nenhum dos casos a venda deixa de subir.

**Aceite:** ✅ com o terminal **offline**, o gerente concede um desconto pelo
painel; o comando fica `pendente`. Ao reconectar, é aplicado **uma vez só**
(reenviá-lo não duplica), aparece no cupom e gera entrada de auditoria
nomeando o gerente remoto **e** o terminal. Um desconto acima do teto do perfil
é recusado pelo terminal, mesmo vindo do painel.

Reproduzido verbatim em `tests/test_remote_commands.py` (22 testes), incluindo
as recusas: assinatura forjada, payload alterado depois de assinado, comando
endereçado a outro terminal, comando vencido, pedido já fechado e caixa
tentando autorizar o que só gerente autoriza.

E percorrido **ponta a ponta sobre HTTP real**, contra um dublê da nuvem numa
porta de verdade: offline → `pendente`; reconecta → aplica uma vez (R$ 28,00 −
25% = R$ 21,00); reentrega o mesmo comando → `accepted=0, applied=0` e o total
não se move; o cupom sai com `Desconto -7,00`; a auditoria grava
`discount_applied` nomeando Bruno Gerente **e** o `device_id`; e os 80% acima do
teto voltam para a nuvem como `refused` **com o motivo**, porque "recusado" sem
motivo faz o gerente tentar de novo igual.

Cobertura por camada: 22 testes de aplicação, 14 de transporte e ciclo
(`test_remote_transport.py`), 9 de formato do fio contra servidor real
(`test_remote_http.py`) e 5 de fiação do `main.py` (`test_bootstrap.py`).

> **Nota de contrato.** A nuvem assina e o terminal confere, com o mesmo
> HMAC implementado nos dois repositórios. Duas versões do mesmo cálculo
> divergem em algum detalhe de serialização, e a divergência aparece como "o
> painel parou de funcionar" numa sexta à noite.
> `test_both_sides_compute_the_same_signature` compara as duas byte a byte —
> quem mexer numa quebra o CI, não a loja.

### Fase 3.6 — Salão de verdade: mesas, conta e gerente no celular (Sprint 11)

**Origem:** relato de uso. Três defeitos que só aparecem com o salão cheio.

**O que estava errado**

| Sintoma | Causa |
|---|---|
| "O app do garçom não funciona" | Não existia app. O servidor local só servia JSON; abrir o endereço do PDV no navegador dava 404. |
| "Abrir mesa não funciona" | Abria **sem trava**: dois garçons criavam duas comandas para a mesma mesa, e a conta saía partida em duas que ninguém junta na hora de cobrar. |
| "Fechar mesa não funciona" | Não existia. O serviço só sabia abrir e lançar item. |
| "Configurar mesas" | Mesa era texto livre em `orders.customer_id`: "mesa 5", "Mesa 5" e "M5" eram três mesas. |

**O que foi feito**

- [x] **Mesa vira entidade** (`store_tables`, migration 4). Índice único por
      `lower(label)` entre as **ativas**. O pedido guarda a **cópia** do
      rótulo: renomear a mesa amanhã não reescreve o cupom de ontem.
- [x] **Desativar, nunca apagar.** Mesa fora do mapa mantém `is_active = 0`;
      apagar a linha arrebentaria as comandas antigas e o relatório de
      faturamento por mesa perderia o passado. Mesa **ocupada não sai** do
      mapa — sumir com ela deixaria a comanda aberta sem porta de entrada.
- [x] **Backfill no upgrade.** Loja instalada não abre na segunda com o salão
      vazio: os rótulos antigos viram mesas e as comandas reencontram a sua.
- [x] **Uma comanda por mesa.** Abrir mesa ocupada devolve 409 **com a comanda
      existente no corpo** — o app abre essa, porque tocar numa mesa ocupada é
      querer lançar nela.
- [x] **Pedir a conta ≠ receber.** O garçom sinaliza; quem recebe é o caixa. Um
      segundo ponto de recebimento — sem gaveta, sem impressora, sem
      conferência de troco — é como o furto de salão entra pela porta da
      frente. O painel do caixa destaca em âmbar quem está esperando.
- [x] **Cancelar comanda e transferir de mesa**, com estorno de fila da cozinha
      e evento `critical` no ledger.
- [x] **App do garçom em web**, servido pelo próprio PDV em `/`. Sem loja de
      aplicativo, sem versão de celular defasada, no aparelho que a pessoa já
      tem. **Não substitui o app nativo** — ver `edge/webapp/__init__.py`.
- [x] **Visualizador de mesas no caixa com pesquisa** e destaque de pedido de
      conta. O caixa recebe de verdade, registra pagamentos e troco, imprime o
      cupom e só então fecha a comanda; pedir a conta no celular nunca equivale
      a marcar a venda como paga.
- [x] **Gorjeta atribuída ao funcionário**, separada do total dos produtos e
      persistida junto ao pagamento. O relatório por funcionário consolida
      vendas, atendimentos, ticket médio e gorjetas sem atribuir resultado ao
      celular compartilhado.
- [x] **Concessão de gerente por aparelho** (`edge/manager.py`): PIN validado
      offline pelo mesmo Argon2id do balcão, vale 10 minutos e só no aparelho
      que a pediu. O celular fica no balcão desbloqueado a noite inteira; uma
      sessão que durasse o turno seria promover o aparelho a gerente.
- [x] **Dividir e juntar conta**, no caixa (Mesas do salão): *pagar parte*
      (os itens escolhidos viram uma comanda da mesma mesa, paga na mesma
      transação, com cupom e venda próprios — a mesa nunca fica com duas
      comandas abertas), *mover itens* entre comandas abertas (o ticket da
      cozinha vai junto) e *juntar comandas* (a de origem fecha zerada e a mesa
      se libera). A soma das contas não muda, o total é recalculado dos itens
      vivos, a venda da parte continua do garçom, e o ledger registra
      `items_transferred` e `order_merged` — junção não é cancelamento.
      Dezenove testes, oito regras verificadas por mutação, e o e2e confere na
      nuvem que cada item está na mesma comanda que no PDV.
- [ ] Mapa de salão com posição das mesas (arrastar no layout).

**Aceite:** ✅ percorrido no navegador contra o servidor real — parear, entrar
como garçom, abrir mesa, lançar item (o item por peso continua ausente do
cardápio do celular), pedir a conta, receber no caixa, registrar gorjeta,
autorizar como gerente, cadastrar mesa nova e cancelar a comanda. A regressão
completa do PDV soma **395 testes**.

### Fase 3.7 — Retaguarda Cloud em Next.js para Coolify ✅ **CONCLUÍDA**

- [x] API reescrita em **Next.js 15 + TypeScript**, com rotas de ativação,
      push/pull de sincronização, comandos remotos, sessão do painel e health.
- [x] Imagem Docker `standalone`, usuário sem privilégio, migrations na subida
      e healthcheck que consulta o PostgreSQL. Pronta para deploy direto no
      Coolify; imagem verificada com aproximadamente **83 MB**.
- [x] PostgreSQL multi-tenant com RLS real. Migrations usam a credencial
      administrativa e a aplicação usa `erp_app` (`NOSUPERUSER NOBYPASSRLS`),
      pois o superusuário fornecido pelo banco gerenciado ignora RLS mesmo com
      `FORCE ROW LEVEL SECURITY`.
- [x] Ledger cloud append-only para a role da aplicação: `INSERT/SELECT`
      permitidos e `UPDATE/DELETE` revogados. A âncora HMAC detecta buraco,
      elo quebrado e reescrita de evento já sincronizado.
- [x] Contrato criptográfico Python ↔ TypeScript testado byte a byte e suíte
      contra PostgreSQL real: **22 testes cloud**, incluindo idempotência,
      rollback atômico, isolamento entre tenants e adulteração do ledger.
- [x] Build e execução da imagem final verificados: migrations automáticas,
      contêiner `healthy`, `/api/health` com `database: ok` e
      `tenant_isolation: ativo`.

**Próximo incremento:** a Fase 3.5.a passa a consumir esta retaguarda para o
dashboard multi-loja e indicadores de frescor; não haverá uma segunda API em
paralelo.

### Fase 4 — Financeiro & Anti-Furto (Sprint 11–13)
- [~] Cashback configurável: fluxo do balcão concluído com identificação do
      cliente, percentual, teto, validade, cupom, crédito idempotente por venda
      e resgate FIFO por lote em ledger append-only. Falta regra por categoria.
- [x] Créditos pré-pagos com **ledger de saldo** — carga autorizada, consumo
      atômico no recebimento e saldo sempre derivado de lançamentos imutáveis.
- [x] Fiado com limite de crédito, bloqueio automático, pagamento FIFO e aging
      de recebíveis vencidos, todo derivado de ledger append-only.
- [x] Níveis de desconto (Bronze / Prata / Ouro / Diamante / Funcionário /
      Dono) configuráveis, com percentuais padrão conservadores,
      atribuição auditada, aplicação automática e validade. A autorização é
      configurável nos demais níveis; **Dono sempre exige senha de
      proprietário**. Somente o proprietário atribui Funcionário ou Dono;
      cancelamento de item continua sendo poder específico do gerente.
      Funcionário e Dono são classificações finais: depois de atribuídos, não
      podem ser trocados por outro nível, com trava no serviço e no banco.
- [x] Conciliação cega de caixa: o núcleo abre a sessão, soma apenas
      dinheiro líquido de troco, grava o declarado antes de revelar o esperado,
      calcula a divergência e fecha auditoria + Outbox na mesma transação. O
      PostgreSQL recebeu a migration 006 e a lista branca do sync. A abertura
      ocorre depois do login e o fechamento F12 exige gerente; depois de fechar,
      a janela termina para impedir venda fora de sessão. A política atual é
      deliberadamente mais rígida que uma tolerância: todo fechamento é autorizado.
- **Aceite:** o operador não obtém o valor esperado do caixa por nenhuma tela,
  relatório ou endpoint antes do fechamento — teste de API incluído.

### Fase 5 — Fiscal (Sprint 14–16)
- [~] NFC-e **server-first** com contingência offline. A nuvem já autentica o
      terminal, reserva a série normal no PostgreSQL, exige cadastro tributário
      completo e chama um serviço fiscal interno com dupla idempotência. O PDV
      só entra em contingência quando a conexão nem foi estabelecida; timeout
      ambíguo vira `unknown` e bloqueia emissão duplicada.
- [x] Serviço fiscal Python privado, sem porta pública, token em comparação
      constante, cofre por referência sem path traversal e estado durável. O
      motor permanece travado para produção até QR Code v3/NT 2025.002 e RJ/SVRS
      passarem em homologação — não inventa XML nem tributação.
- [x] Interface `FiscalProvider` no Next.js: permite substituir PyNFe por motor
      JavaScript ou API fiscal sem alterar contratos, séries ou o PDV.
- [x] Numeração de série por PDV, nunca compartilhada entre estações. A reserva
      usa `BEGIN IMMEDIATE`, chave única por terminal/modelo/série/número e
      devolve o mesmo documento quando a mesma venda é reenviada.
- [x] **Reconciliação do documento preso.** O serviço fiscal distingue
      `NOT_FOUND` (a chamada nunca chegou — seguro retransmitir com o mesmo
      número) de `IN_FLIGHT` (pode ter chegado à SEFAZ — não retransmite). Antes
      os dois eram `unknown` indistintos, e o documento cujo processo caiu entre
      reservar e transmitir ficava `processing` para sempre.
- [x] **Travas antes da reserva:** sem serviço fiscal configurado a emissão
      responde 503 sem consumir número, e `production` exige
      `FISCAL_PRODUCTION_ENABLED` — o motor ainda travado não pode queimar
      numeração real que depois exigiria inutilização.
- [x] **Retaguarda saudável sem o fiscal.** As variáveis do serviço fiscal
      deixaram de ser obrigatórias no processo; `/api/health` publica
      `fiscal: desligado | somente homologação | produção liberada`.
- **Aceite parcial entregue:** 16 vendas reservadas concorrentemente recebem
  os números 1–16 sem repetição; 12 tentativas concorrentes da mesma venda
  geram um único documento e consomem um único número. No PostgreSQL real, o
  reenvio chama o provedor uma vez; timeout vira `unknown` e continua sem uma
  segunda emissão. Documento preso é retransmitido com o mesmo número e o
  motor roda uma vez; `IN_FLIGHT` nunca é retransmitido. As quatro regras
  foram verificadas por mutação.

- [x] **Venda com total zero não emite NFC-e** (desconto de 100% ou produto de
      preço zero). Desfecho `not_required` na nuvem, no terminal e na
      contingência offline, sem consumir número da série; 99% de desconto
      continua emitindo, e total negativo é recusado como defeito. A cortesia
      segue rastreável pela autorização do desconto no ledger de auditoria.
- [x] **DANFE NFC-e 80 mm** com as nove divisões do manual, contingência e
      homologação visíveis, e recusa de documento incoerente com a própria
      chave de acesso. Dígito verificador conferido contra o PyNFe em 5.000
      chaves; as oito recusas verificadas por mutação.
- [x] **Cadastro fiscal na tela do dono.** Emitente, série, referências do
      A1/CSC no cofre e perfil tributário de cada produto, com a lista do que
      ainda impede a primeira nota. O certificado e a senha nunca passam pela
      API; CNPJ, município, CFOP e CSOSN/CST × regime são conferidos no
      cadastro, e não na venda. Só o dono acessa, e tudo é auditado. Nove
      regras verificadas por mutação.
- [x] **Autoteste do pacote cobre o fiscal.** `PDV.exe --selftest` carrega a
      tabela de municípios do PyNFe (lida por caminho, invisível ao
      PyInstaller), confere a licença LGPL embarcada e monta um DANFE em PC850.
      Sem isso, um pacote incompleto funcionaria por meses e quebraria na
      primeira NFC-e da loja.

### Fase 0 — CI (complemento)
- [x] GitHub Actions com as três suítes, Postgres real e **teste pulado
      reprova**. Os pins do `requirements.txt` passaram a ser as versões
      testadas e empacotadas — antes não eram, e o primeiro nem instalava em
      Python 3.14. Verificado num ambiente limpo: 457 testes com os pins.

### Fase 6 — IA & Canais (Sprint 17–20)
- [ ] Cardápio QR com upsell contextual.
- [ ] WhatsApp Cloud API + LLM anotador (com confirmação humana obrigatória).
- [ ] Previsão de demanda (baseline sazonal + gradient boosting).

---

## 4. Invariantes do Sistema (nunca violar)

1. **Toda** tabela de negócio carrega `tenant_id NOT NULL`. Sem exceção.
2. **Toda** query no backend passa por RLS; `SET LOCAL app.tenant_id` por request.
3. Dinheiro: `NUMERIC(14,2)` no Postgres, `INTEGER` em centavos no SQLite,
   `Decimal` em Python. **Float para dinheiro é proibido.**
4. Insumos: `INTEGER` em **miligramas** ou **mililitros**. Elimina erro de
   arredondamento acumulado na baixa fracionada.
5. Identificadores criados offline são **UUIDv7** gerados no cliente.
6. Nada é apagado fisicamente: `deleted_at` + replicação da exclusão.
7. Toda mutação de dinheiro grava em `audit_ledger` com `prev_hash` / `hash`.
8. O relógio do caixa não é confiável: gravar `created_at` local **e**
   `server_received_at`.
9. Impressão e leitura serial nunca bloqueiam a UI thread.
10. O PDV nunca exige internet para vender.

---

## 5. Definition of Done

- [ ] Tipagem estrita (`mypy --strict`, `tsc --noEmitOnError`).
- [ ] Teste unitário para toda regra de dinheiro e de estoque.
- [ ] Teste de integração de sync com simulação de falha de rede.
- [ ] Migration reversível (Alembic no cloud, migrations versionadas no SQLite).
- [ ] Entrada de auditoria para toda operação sensível.
- [ ] `plan.md` e `tech_stack.md` atualizados **no mesmo commit** da mudança.

---

## 6. Riscos Mapeados

| Risco | Impacto | Mitigação |
|---|---|---|
| Duplicidade de pedido no sync | Alto | Outbox + `client_uuid` idempotente + ACK por item |
| Protocolo de balança variar por firmware | Médio | Camada `ScaleProtocol` plugável + modo simulado |
| Driver USB da impressora travar | Médio | Fila de impressão em thread + timeout + retry |
| Relógio do caixa incorreto | Alto | `server_received_at` como verdade + alerta de drift |
| Fraude do operador com acesso ao SQLite | Alto | Hash chain + espelho no servidor + detecção de gap |
| Custo de LLM no WhatsApp | Médio | Cache de intenção + roteamento por complexidade |
