# tech_stack.md — Decisões Técnicas

> **Contrato de contexto.** Este arquivo define *como* construímos. Toda escolha
> aqui é uma decisão fechada com justificativa e alternativa descartada. Trocar
> qualquer item exige atualizar esta tabela **no mesmo commit**.
> Escopo e ordem estão em [`plan.md`](./plan.md).

---

## 1. Visão Geral da Topologia

```
                    ┌──────────────────────────────────────┐
                    │        CLOUD (multi-tenant)          │
                    │  Next.js 15 + PostgreSQL 17 (RLS)    │
                    │  API/painel + fiscal privado         │
                    └───────▲──────────────────▲───────────┘
                            │ HTTPS/WSS        │ HTTPS
                            │ (sync em lote)   │
        ┌───────────────────┴────────┐    ┌────┴─────────────────┐
        │   DESKTOP PDV (Windows)    │    │ Painel no mesmo Next  │
        │   PySide6 + SQLite (WAL)   │    │   Retaguarda / BI     │
        │   FastAPI local + WS       │    └───────────────────────┘
        │   ├─ Serial  → Balança     │
        │   └─ USB RAW → TM-T20X     │
        └───────▲────────────────────┘
                │ LAN (mDNS + HTTP/WS)
        ┌───────┴────────────────────┐
        │  MOBILE GARÇOM (RN)        │
        │  SQLite (WatermelonDB)     │
        └────────────────────────────┘
```

O Desktop PDV é **edge server**: quando a internet cai, ele continua servindo o
mobile e o KDS pela rede local. A nuvem é a fonte da verdade *eventual*, nunca a
dependência de tempo real da operação de venda.

---

## 2. Cloud Backend

| Camada | Escolha | Por quê | Alternativa descartada |
|---|---|---|---|
| Linguagem | TypeScript 5 / Node 22 | API e painel no mesmo deploy, contrato tipado | Backend Python separado (dois deploys para CRUD/painel) |
| Framework | Next.js 15 | Route handlers + React no mesmo artefato standalone | FastAPI para toda a nuvem |
| SQL | `postgres` com transações explícitas | RLS, `FOR UPDATE` e upserts ficam visíveis | ORM que esconda limites transacionais |
| Banco | PostgreSQL 17 | RLS nativo = isolamento multi-tenant na camada mais baixa | MySQL (sem RLS) |
| Fiscal | Next orquestrador + FastAPI/PyNFe privado | A1 fora do processo público e motor substituível | Biblioteca JS ainda sem contingência/QR v3/RJ homologados |
| Cache/Fila | PostgreSQL nesta fase | Menos infraestrutura até fan-out justificar Redis | Redis prematuro |
| Realtime local | FastAPI/WebSocket no PDV | KDS e garçom continuam mesmo sem internet | WebSocket cloud como dependência da loja |
| Storage | S3-compatible (planejado) | XMLs fiscais e imagens fora do banco após autorização | Filesystem do contêiner |
| Observab. | Logs estruturados; OTel planejado | Deploy atual continua simples sem fechar a porta à telemetria | Logs com segredos/corpos fiscais |

### Estratégia Multi-Tenant
**Shared database, shared schema, com RLS.** Única coluna `tenant_id` + policy:

```sql
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON orders
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

Middleware executa `SET LOCAL app.tenant_id = :claim` a cada request, dentro da
transação. Aplicação **não** filtra por `tenant_id` manualmente — se o dev
esquecer o filtro, o banco protege. Tenants Enterprise podem migrar para schema
dedicado sem mudar o código (mesmo modelo, `search_path` distinto).

---

## 3. Desktop PDV (Windows) — o núcleo desta fase

| Camada | Escolha | Por quê |
|---|---|---|
| UI | **PySide6 (Qt 6.7)** | LGPL — permite distribuição comercial fechada sem licença paga, ao contrário do PyQt6 (GPL/comercial). API praticamente idêntica |
| Banco local | SQLite 3.45 em modo **WAL** | Leitura concorrente durante escrita; `synchronous=FULL` para não perder venda em queda de energia |
| Acesso a dados | `sqlite3` stdlib + repositórios tipados | Sem ORM: o schema offline é pequeno e o controle transacional precisa ser explícito |
| Serial | **pyserial 3.5** | Padrão de fato para COM/USB-serial no Windows |
| Impressão | **win32print (RAW)** como primário, `python-escpos` como alternativo | Ver §3.2 |
| Concorrência | `QThread` + sinais Qt | Balança e impressora fora da UI thread, sem `asyncio` no loop do Qt |
| HTTP sync | `httpx` (async) em worker próprio | Timeouts e retry com backoff |
| Server local | FastAPI + uvicorn embarcado em thread | Atende o mobile e o KDS via LAN |
| Empacotamento | PyInstaller (onedir) + Inno Setup | Instalação com serviço de auto-start e updater |
| Tipagem | `mypy --strict`, `Decimal` para dinheiro | Invariante 3 do `plan.md` |

### 3.1 Balança de Checkout (porta serial)

Driver com **protocolo plugável** (`ScaleProtocol`), porque o formato do quadro
varia por fabricante e por *firmware*:

| Modelo | Modo | Quadro típico | Default serial |
|---|---|---|---|
| **Toledo Prix 3 / 4** | Requisição: host envia `ENQ` (0x05) | `STX` + 5–6 dígitos ASCII + `ETX` | 9600, 8N1 |
| **Filizola** | Streaming contínuo | `STX` + peso + (tara) + `ETX` | 9600, 8N1/8N2 |
| **Urano (POP/UDC)** | Requisição por `ENQ` | `STX` + peso + `ETX` | 9600, 8N1 |

> ⚠️ **Validar contra o manual do equipamento.** Número de dígitos, presença de
> byte de status, ponto decimal implícito e paridade mudam entre versões de
> firmware. O código isola isso em uma classe por protocolo justamente para que
> ajustar um modelo não toque nos demais. Há um `SimulatedScale` para
> desenvolvimento sem hardware.

Regras do driver:
- Leitura contínua em `QThread`, nunca na UI thread.
- **Peso só é aceito quando estável**: N leituras idênticas consecutivas
  (`STABLE_READINGS`) dentro da janela de tolerância.
- Peso trafega como `int` em **gramas**; `Decimal` só na conversão para preço.
- Estados explícitos: `STABLE`, `UNSTABLE`, `OVERLOAD`, `NEGATIVE`, `ERROR`.

### 3.2 Impressora Térmica Epson TM-T20X (80 mm)

Duas rotas, escolhidas por ambiente:

1. **`win32print` com RAW datatype (primário no Windows).** O driver Epson da
   máquina permanece instalado; enviamos bytes ESC/POS crus via
   `StartDocPrinter(..., ("PDV", None, "RAW"))`. Vantagem: não exige troca de
   driver, funciona com a impressora compartilhada e respeita a fila do Windows.
2. **`python-escpos` via USB (libusb).** Exige substituir o driver Epson por
   WinUSB/libusb (Zadig) — o que **quebra** o uso da impressora por outros
   programas. Reservado para Linux ou terminais dedicados.

Ambas recebem **o mesmo payload de bytes**, gerado por um construtor puro
(`EscPosBuilder`) sem efeito colateral — o que torna o layout testável sem
hardware (compara-se o `bytes` esperado).

Comandos usados (Epson ESC/POS):

| Função | Bytes | Observação |
|---|---|---|
| Inicializar | `ESC @` (`1B 40`) | Limpa buffer e formatação |
| Code page | `ESC t 2` (`1B 74 02`) | PC850 Multilingual → acentuação pt-BR |
| Alinhamento | `ESC a n` (`1B 61 n`) | 0=esq, 1=centro, 2=dir |
| Negrito | `ESC E n` (`1B 45 n`) | |
| Tamanho | `GS ! n` (`1D 21 n`) | nibble alto = largura, baixo = altura |
| Corte parcial | `GS V 66 n` (`1D 56 42 n`) | `n` = avanço antes da guilhotina |
| Gaveta | `ESC p m t1 t2` (`1B 70 00 19 FA`) | Pino 2, pulso ~25/250 ms |

Largura útil: **48 colunas** em Fonte A (80 mm). O layout é montado em colunas
fixas para o total nunca "vazar" a linha.

### 3.3 Baixa Fracionada de Estoque

Produto pesável → item de venda em gramas → **ficha técnica** converte para
insumos. Toda a matemática ocorre em **inteiros (mg)** e só o preço usa `Decimal`:

```
consumo_mg = receita.qty_per_base_mg * peso_gramas / receita.base_qty_g
```

O arredondamento final usa `ROUND_HALF_UP` e o resíduo é descartado apenas no
último passo — nunca por item intermediário.

---

## 4. Mobile Garçom

| Camada | Escolha | Por quê |
|---|---|---|
| Framework | **React Native 0.75 + TypeScript** | Reaproveita tipos/DTOs do backend via pacote compartilhado; pool de devs maior no BR |
| Navegação | Expo Router | Deep link para mesa/comanda |
| Estado | Zustand + TanStack Query | Cache offline com `persistQueryClient` |
| Banco local | **WatermelonDB (SQLite)** | Sync engine com push/pull incremental já no formato do nosso Outbox |
| Descoberta LAN | `react-native-zeroconf` (mDNS `_pdvedge._tcp`) | Encontra o PDV sem IP fixo |
| Transporte | `httpx`-like via `ky` + WebSocket nativo | Mesma API em LAN e Cloud, só muda o `baseURL` |
| Impressão remota | Solicita ao PDV via LAN | O celular nunca fala com a impressora |

**Roteamento de conectividade (ordem de tentativa):** LAN (PDV) → Cloud → fila
local. Um pedido lançado sempre resulta em registro local com `client_uuid`;
o transporte é detalhe.

Flutter foi considerado (melhor performance de UI e binário único). Descartado
por não reaproveitar os tipos TypeScript do backend/web e por dobrar a
superfície de manutenção da equipe atual.

---

## 5. Sincronização — o coração do offline-first

**Padrão Outbox + idempotência por chave de cliente.**

1. Toda escrita local grava a entidade **e** uma linha em `sync_outbox`, na
   mesma transação SQLite.
2. O worker lê o outbox em ordem (`seq` crescente), monta lote de até 200 itens
   e faz `POST /sync/push` com `Idempotency-Key` do lote.
3. O servidor aplica o lote em **uma transação**, com
   `INSERT ... ON CONFLICT (tenant_id, client_uuid) DO NOTHING`, e responde o
   status por item.
4. O cliente só marca `is_synced = 1` / `synced_at` para os itens com ACK.
   Falhou no meio? Reenvia — `client_uuid` garante que nada duplica.
5. Pull incremental: `GET /sync/pull?since=<updated_at>&cursor=<server_seq>`.

**Regras de conflito:**
- Vendas, pagamentos, movimentos de estoque e auditoria são **append-only** →
  não existe conflito, apenas deduplicação.
- Cadastros (produtos, preços, clientes) usam **LWW** por `updated_at`, com
  desempate determinístico por `server_seq`. Cloud vence empate.
- Estoque nunca é sincronizado como saldo absoluto, e sim como **movimentos**.
  O saldo é derivado. Isso torna a fusão de duas lojas offline trivialmente correta.

---

## 6. IA

| Uso | Modelo / Técnica | Onde roda |
|---|---|---|
| Upsell no cardápio QR | `claude-haiku-4-5` com catálogo no prompt + regras de margem | Cloud (edge cache) |
| WhatsApp anotador de pedidos | `claude-sonnet-5` com tool use (`add_item`, `confirm_order`) | Cloud + Celery |
| Previsão de demanda | Baseline sazonal (média móvel + dia da semana + feriado) → LightGBM quando houver ≥ 90 dias de histórico | Celery nightly |

Regra dura: **LLM nunca fecha pedido sozinho.** Ele monta o rascunho; a
confirmação é do cliente (WhatsApp) ou do operador (PDV). Toda saída de LLM que
vira transação passa por validação de schema contra o catálogo real.

---

## 7. Segurança

- **Autenticação:** JWT curto (15 min) + refresh rotativo; device binding por PDV.
- **Autorização:** RBAC (`owner`, `manager`, `cashier`, `waiter`, `kitchen`) +
  permissões finas para operações sensíveis.
- **Senha de gerente:** desconto acima do teto, cancelamento de item impresso,
  sangria e abertura de gaveta fora de venda exigem credencial de supervisor —
  validada **localmente** (Argon2id do hash sincronizado) para funcionar offline.
- **Ledger imutável:** `audit_ledger` encadeado por SHA-256
  (`hash = SHA256(prev_hash || payload_canônico)`). O servidor revalida a cadeia
  no sync e sinaliza *gap* ou adulteração. Banco local é considerado hostil.
- **Conciliação cega:** o endpoint de fechamento não retorna o valor esperado
  antes do POST da contagem. A restrição está na API, não na UI.
- **Criptografia:** TLS 1.3 em trânsito; SQLite local com SQLCipher; segredos em
  DPAPI (Windows) / secret manager (cloud).
- **LGPD:** dados de cliente pseudonimizados em relatórios, retenção configurável
  por tenant e exportação/exclusão sob demanda.

---

## 8. Qualidade e CI

| Item | Ferramenta |
|---|---|
| Format/Lint Python | Ruff |
| Tipos Python | mypy `--strict` |
| Testes Python | pytest + pytest-qt + hypothesis (regras de arredondamento) |
| Lint/Tipos TS | ESLint + tsc |
| Testes mobile | Jest + Detox |
| Migrations | Alembic (cloud) + migrations SQL versionadas (SQLite) |
| CI | GitHub Actions: matriz Linux (cloud) + Windows (desktop) |
| Entrega desktop | PyInstaller + Inno Setup + canal de update assinado |

Nenhum PR entra sem: tipos verdes, testes verdes e `plan.md`/`tech_stack.md`
coerentes com a mudança.
