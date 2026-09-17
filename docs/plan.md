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
      DPAPI em escopo de máquina. Nunca trafega pela rede, nunca entra no banco
      que ele protege.
- [ ] **Ativação do terminal**: tela pedindo o código do tenant, que provisiona
      `device_id` e o token de sincronização junto à retaguarda.
- [x] **Teste de fumaça pós-instalação**: verifica gravação no banco e modo WAL,
      integridade da cadeia de auditoria, resposta da balança, impressão de um
      cupom de teste (com acentuação, corte e pulso de gaveta) e presença de
      catálogo. Cada verificação é independente — uma balança desligada não
      esconde que a impressora também está sem papel. Relatório na tela e em
      `ProgramData\ERPFood\PDV\logs\setup.log`; saída 0/2/3 para o instalador.
- [x] Atalho "Reconfigurar periféricos" no menu Iniciar (`PDVSetup.exe
      --detect-only`): troca de balança ou de porta USB se resolve sem
      reinstalar nada.
- [ ] Atualização in-place preservando o banco e a fila de sincronização.
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
- [ ] Descoberta do PDV na LAN via mDNS (`_pdvedge._tcp`).
- [ ] Desktop expõe API local (FastAPI embarcado) + WebSocket para o KDS.
- [ ] Mobile decide rota: LAN se disponível, senão Cloud; fila local se ambos caírem.
- [ ] KDS com tempo por item, bump/recall e alerta de atraso.
- **Aceite:** pedido lançado no celular aparece no KDS em < 1 s com Wi-Fi isolado
  (sem rota para a internet).

### Fase 3.5 — Painel Administrativo Remoto (Sprint 10–12)

**Requisito do cliente:** acompanhar as estatísticas atualizadas de qualquer
lugar pelo navegador e **agir à distância** — alterar um pedido, conceder um
desconto, cancelar um item — sem precisar estar na loja.

#### 3.5.a — Telemetria ao vivo (caminho de leitura)
- [ ] Dashboard web por loja: faturamento do dia, ticket médio, vendas/hora,
      ranking de produtos, CMV e margem — atualizando via WebSocket.
- [ ] Visão consolidada multi-loja para o dono (respeitando `tenant_id`).
- [ ] Estado operacional de cada terminal: online/offline, **itens pendentes de
      sincronização**, último ACK, drift de relógio, papel da impressora.
- [ ] Indicador explícito de **frescor do dado**: "atualizado há 8 s" vs
      "terminal offline há 40 min — números podem estar defasados". Dashboard
      que mente sobre estar atualizado é pior que dashboard sem dado.
- [ ] Alertas: caixa mudo, divergência de conciliação, pico de cancelamentos,
      estoque negativo, cadeia de auditoria acusando adulteração.

#### 3.5.b — Comandos remotos (caminho de escrita)
> ⚠️ **Inverte o modelo de confiança.** Até aqui o PDV só *enviava*. Aceitar
> comandos de fora transforma o terminal em alvo: quem comprometer o painel
> passa a conceder descontos e cancelar itens em todas as lojas. Por isso o
> caminho de escrita tem exigências que o de leitura não tem.

- [ ] **Padrão Inbox** (espelho do Outbox): o comando entra numa fila no
      terminal e é aplicado **uma única vez**, com `command_uuid` idempotente.
      Reenvio por timeout não concede o desconto duas vezes.
- [ ] **Offline-first também na ida:** terminal sem internet recebe o comando
      ao reconectar. O painel mostra `pendente` → `entregue` → `aplicado` ou
      `recusado`, nunca um "sucesso" otimista.
- [ ] **Mesmos tetos do presencial.** Desconto remoto respeita
      `discount_tiers.max_discount_cents` e o limite do perfil de quem emitiu.
      Estar longe não amplia poder — se não pode no balcão, não pode remoto.
- [ ] **Escopo restrito:** só pedido **em aberto**. Venda fechada altera-se por
      estorno/nova venda; documento fiscal transmitido, só por cancelamento
      fiscal. O painel nunca reescreve o passado.
- [ ] **Auditoria com dupla identidade:** cada evento grava o `actor` remoto, o
      `device_id` alvo, IP e canal (`remote_panel`). Relatório de cancelamentos
      separa presencial de remoto — senão o painel vira a rota limpa para o
      mesmo furto que o M09 combate.
- [ ] **Confirmação no terminal para operações de risco:** cancelar item já
      impresso ou abrir gaveta exige aceite do operador presente. Abrir gaveta
      remotamente sem ninguém por perto é convite a furto.
- [ ] **Autenticação forte do emissor:** 2FA obrigatório para perfis com poder
      de comando remoto; sessão curta; assinatura do comando verificada no
      terminal (o PDV não obedece a quem não prova quem é).
- [ ] **Rate limit e kill switch:** teto de comandos por minuto por operador e
      botão do dono para desligar o canal remoto de uma vez.

**Aceite:** com o terminal **offline**, o gerente concede um desconto pelo
painel; o comando fica `pendente`. Ao reconectar, é aplicado **uma vez só**
(reenviá-lo não duplica), aparece no cupom e gera entrada de auditoria
nomeando o gerente remoto **e** o terminal. Um desconto acima do teto do perfil
é recusado pelo terminal, mesmo vindo do painel.

### Fase 4 — Financeiro & Anti-Furto (Sprint 11–13)
- [ ] Cashback configurável (percentual, teto, validade, regra por categoria).
- [ ] Créditos pré-pagos com **ledger de saldo** — nunca uma coluna mutável de saldo.
- [ ] Fiado com limite de crédito, bloqueio automático e aging de recebíveis.
- [ ] Níveis de desconto (Diamante / Funcionário / Dono) com teto e autorização.
- [ ] Conciliação cega de caixa: o operador não vê o esperado até fechar.
- **Aceite:** o operador não obtém o valor esperado do caixa por nenhuma tela,
  relatório ou endpoint antes do fechamento — teste de API incluído.

### Fase 5 — Fiscal (Sprint 14–16)
- [ ] NFC-e com contingência offline e transmissão posterior.
- [ ] Numeração de série por PDV, nunca compartilhada entre estações.

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
