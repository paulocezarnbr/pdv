# project_structure.md — Estrutura Modular do Monorepo

Monorepo único, três alvos de build independentes, um pacote de domínio
compartilhado entre cloud e desktop (ambos Python → a regra de preço/estoque é
escrita **uma vez**).

```
pdv/
├── docs/                              # Development Context Files — leitura obrigatória
│   ├── plan.md                        # O QUE e em qual ordem
│   ├── tech_stack.md                  # COMO e por quê
│   ├── der.md                         # Modelo de dados + campos de sync
│   ├── project_structure.md           # Este arquivo
│   └── adr/                           # Architecture Decision Records
│       └── 0001-offline-first-outbox.md
│
├── apps/
│   ├── cloud-api/                     # ───── SaaS Cloud Backend (FastAPI) ─────
│   │   ├── src/erp/
│   │   │   ├── main.py
│   │   │   ├── config.py
│   │   │   ├── core/                  # M01 — tenancy, auth, RBAC, RLS middleware
│   │   │   │   ├── tenancy.py         # SET LOCAL app.tenant_id por request
│   │   │   │   ├── security.py        # JWT, Argon2id, device binding
│   │   │   │   └── modules.py         # feature flags por tenant (tenant_modules)
│   │   │   ├── modules/               # um pacote por bounded context
│   │   │   │   ├── catalog/           # M02  router / service / repository / schemas
│   │   │   │   ├── inventory/         # M03  ficha técnica, baixa, CMV
│   │   │   │   ├── sales/             # M04  pedidos, pagamentos
│   │   │   │   ├── sync/              # M05  push/pull, idempotência, replay
│   │   │   │   │   ├── router.py      #      POST /sync/push · GET /sync/pull
│   │   │   │   │   ├── merge.py       #      LWW + append-only
│   │   │   │   │   └── idempotency.py
│   │   │   │   ├── dining/            # M06  mesas e comandas
│   │   │   │   ├── kds/               # M07  WebSocket + Redis pub/sub
│   │   │   │   ├── finance/           # M08  cashback, pré-pago, fiado, tiers
│   │   │   │   ├── security_audit/    # M09  ledger, conciliação cega
│   │   │   │   ├── fiscal/            # M10  NFC-e / SAT
│   │   │   │   ├── qrmenu/            # M11  cardápio + upsell IA
│   │   │   │   ├── whatsapp/          # M12  Cloud API + LLM anotador
│   │   │   │   ├── forecasting/       # M13  previsão de demanda
│   │   │   │   └── reporting/         # M14  BI
│   │   │   └── infra/                 # db, redis, storage, celery, telemetry
│   │   ├── migrations/                # Alembic
│   │   └── tests/
│   │
│   ├── web-admin/                     # ───── Retaguarda (Next.js 14) ─────
│   │   └── src/{app,features,components,lib}/
│   │
│   ├── desktop-pdv/                   # ───── App Desktop Python (Windows) ─────
│   │   ├── main.py                    # bootstrap: DB → serviços → janela
│   │   ├── requirements.txt
│   │   ├── src/pdv/
│   │   │   ├── config.py              # AppConfig, ScaleConfig, PrinterConfig
│   │   │   ├── domain/
│   │   │   │   ├── models.py          # dataclasses frozen + Money/Weight
│   │   │   │   └── errors.py          # exceções de domínio
│   │   │   ├── hardware/
│   │   │   │   ├── scale/
│   │   │   │   │   ├── base.py        # ScaleProtocol, ScaleReading, ScaleStatus
│   │   │   │   │   ├── protocols.py   # Toledo Prix 3 · Filizola · Urano
│   │   │   │   │   ├── serial_scale.py# driver pyserial + estabilidade
│   │   │   │   │   └── worker.py      # QThread + sinais Qt
│   │   │   │   └── printer/
│   │   │   │       ├── escpos.py      # EscPosBuilder (puro, testável)
│   │   │   │       ├── layout.py      # cupom 80 mm / 48 colunas
│   │   │   │       └── backends.py    # win32print RAW · python-escpos · arquivo
│   │   │   ├── data/
│   │   │   │   ├── schema.sql         # espelho offline do der.md
│   │   │   │   ├── database.py        # conexão WAL + migrations
│   │   │   │   └── repositories.py    # produtos, receitas, estoque, vendas, outbox
│   │   │   ├── services/
│   │   │   │   ├── pricing.py         # peso → preço (Decimal, ROUND_HALF_UP)
│   │   │   │   ├── stock.py           # baixa fracionada por ficha técnica
│   │   │   │   ├── audit.py           # ledger SHA-256 encadeado
│   │   │   │   └── checkout.py        # orquestra a venda em 1 transação
│   │   │   ├── fiscal/
│   │   │   │   └── service.py          # série/número atômicos + contingência
│   │   │   ├── sync/
│   │   │   │   ├── outbox.py
│   │   │   │   ├── client.py          # httpx + backoff
│   │   │   │   └── worker.py          # thread de sincronização
│   │   │   ├── localserver/           # FastAPI embarcado p/ mobile e KDS na LAN
│   │   │   │   ├── api.py
│   │   │   │   ├── ws_kds.py
│   │   │   │   └── discovery.py       # mDNS _pdvedge._tcp
│   │   │   └── ui/
│   │   │       ├── counter_window.py  # tela do caixa de balcão
│   │   │       └── widgets/
│   │   ├── tests/
│   │   └── packaging/                 # PyInstaller spec + Inno Setup
│   │
│   └── mobile-waiter/                 # ───── App do Garçom (React Native) ─────
│       └── src/
│           ├── features/{tables,orders,menu,auth}/
│           ├── db/                    # WatermelonDB schema + sync adapter
│           ├── network/               # roteador LAN ↔ Cloud + mDNS
│           └── ui/
│
├── packages/                          # ───── Compartilhado ─────
│   ├── domain-py/                     # regras usadas por cloud-api E desktop-pdv
│   │   └── src/erp_domain/{pricing.py,recipe.py,discounts.py,money.py}
│   ├── contracts/                     # OpenAPI + JSON Schema (fonte da verdade)
│   └── ts-types/                      # tipos gerados p/ web-admin e mobile
│
├── infra/                             # Terraform, Docker, K8s, GitHub Actions
└── tools/                             # geradores de código, seeds, simuladores
    └── scale_simulator.py             # emula balança numa COM virtual
```

## Regras de dependência

```
apps/desktop-pdv ──> packages/domain-py <── apps/cloud-api
apps/mobile-waiter ──> packages/ts-types <── packages/contracts
```

1. `packages/domain-py` **não importa** nada de `apps/`. É puro: sem Qt, sem I/O,
   sem SQL. Assim a regra de preço roda igual nos dois lados.
2. Em `apps/cloud-api` (Next.js), as rotas de `src/app/api/` não conversam
   entre si: elas chamam `src/lib/`. Uma rota que importa outra rota acopla
   dois contratos HTTP que deveriam poder mudar separado.
3. `hardware/` no desktop não conhece `ui/`. A UI assina sinais; o driver não
   sabe que existe tela.
4. `contracts/` gera os tipos TS. Ninguém escreve DTO à mão duas vezes.
