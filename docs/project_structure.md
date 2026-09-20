# project_structure.md — Estrutura Modular do Monorepo

Os três processos têm builds e ciclos de deploy independentes. Dependências
apontam para dentro do próprio processo; integração entre eles ocorre por
contratos HTTP versionados, nunca importando código de outro app.

```text
pdv/
├── docs/                              # contexto arquitetural obrigatório
│   ├── plan.md
│   ├── tech_stack.md
│   ├── der.md
│   └── fiscal_architecture.md
├── apps/
│   ├── cloud-api/                     # Next.js 15 + TypeScript + PostgreSQL
│   │   ├── src/app/api/               # rotas device/panel/sync/fiscal
│   │   ├── src/lib/                   # auth, RLS, merge, fiscal providers
│   │   ├── migrations/                # SQL ordenado, aplicado no deploy
│   │   ├── tests/
│   │   └── Dockerfile
│   ├── fiscal-service/                # FastAPI interno, sem porta pública
│   │   ├── src/fiscal_service/        # auth, engine, cofre e idempotência
│   │   ├── tests/
│   │   └── Dockerfile
│   └── desktop-pdv/                   # Python/PySide6 offline-first
│       ├── main.py
│       ├── src/pdv/
│       │   ├── domain/                # tipos e erros sem I/O
│       │   ├── data/                  # SQLite, migrations, repositories
│       │   ├── services/              # checkout, estoque, caixa, financeiro
│       │   ├── hardware/              # balança e ESC/POS
│       │   ├── sync/                  # outbox/inbox e transporte cloud
│       │   ├── remote/                # comandos administrativos assinados
│       │   ├── edge/                  # app garçom/KDS HTTPS na LAN
│       │   ├── fiscal/                # cloud-first + contingência local
│       │   └── ui/                    # PySide6
│       ├── packaging/                 # PyInstaller + Inno Setup + ACL
│       └── tests/
└── README.md
```

## Regras de dependência

- `domain` não importa banco, Qt, HTTP ou hardware.
- UI chama serviços; não escreve SQL.
- Hardware fica atrás de protocolos injetáveis e testáveis.
- Toda escrita offline enfileira outbox na mesma transação.
- Cloud resolve `tenant_id` da credencial; nunca confia no corpo.
- Next.js não lê A1. Envia apenas `certificate_ref` ao serviço fiscal interno.
- `fiscal-service` não é exposto pelo proxy e nunca recebe token de terminal.
- Motor fiscal é substituível por `FiscalProvider`; série e estados não mudam.
- PDV só emite contingência após falha de conexão comprovadamente anterior ao
  envio. Resultado ambíguo permanece `unknown`.
