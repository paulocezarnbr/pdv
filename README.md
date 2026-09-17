# ERP SaaS Food Service — Arquitetura Híbrida Offline-First

ERP multi-tenant e modular para restaurantes, lanchonetes e confeitarias, com
três clientes sobre um único backend: **web na nuvem**, **PDV desktop em Python**
(offline-first, com periféricos físicos) e **app do garçom** em mobile.

> **Princípio nº 1: o restaurante nunca para.** Queda de internet degrada
> funcionalidades acessórias (BI, WhatsApp, cardápio QR), nunca a operação de
> venda. O PDV desktop é autossuficiente e atua como *edge server* da loja.

---

## Documentos de contexto (leia antes de codar)

Este projeto é guiado por *Development Context Files*. Nenhuma feature entra no
código sem estar refletida neles.

| Arquivo | Papel |
|---|---|
| [`docs/plan.md`](docs/plan.md) | **O quê** e em qual ordem — módulos, roadmap, invariantes, DoD |
| [`docs/tech_stack.md`](docs/tech_stack.md) | **Como** e por quê — decisões fechadas com alternativa descartada |
| [`docs/der.md`](docs/der.md) | **O dado** — DER completo com os campos de sincronização |
| [`docs/project_structure.md`](docs/project_structure.md) | Estrutura modular do monorepo e regras de dependência |

Regra: alterou escopo ou stack? Atualize o arquivo correspondente **no mesmo
commit** da mudança de código.

---

## Estado atual

✅ **Fase 1 — PDV Balcão Offline** implementada e testada (33 testes passando).

O que já funciona ponta a ponta, sem internet:

1. Leitura da balança via porta serial (Toledo Prix 3 · Filizola · Urano), com
   detecção de peso estável em thread separada.
2. Cálculo de preço por peso com tara descontada e `ROUND_HALF_UP` em `Decimal`.
3. **Baixa fracionada** de insumos pela ficha técnica, em miligramas inteiros,
   com fator de rendimento e percentual de perda.
4. Ledger de auditoria **imutável encadeado por SHA-256** (anti-furto).
5. Impressão **ESC/POS bruta** para Epson TM-T20X (80 mm) com guilhotina e
   acionamento de gaveta.
6. Tudo isso numa **única transação SQLite** + enfileiramento no outbox de sync.

🔜 Próximo: Fase 2 — worker de sincronização com o PostgreSQL da nuvem.

---

## Rodando o PDV desktop

```bash
cd apps/desktop-pdv
pip install -r requirements.txt
PYTHONPATH=src python main.py
```

Sem hardware conectado, sobe com **balança simulada** e impressora em arquivo
(os cupons saem em `out/` como `.bin` e `.txt`).

Com hardware real:

```bash
PDV_SCALE_PROTOCOL=toledo_prix3 PDV_SCALE_PORT=COM3 PDV_PRINTER_BACKEND=win32raw PYTHONPATH=src python main.py
```

### Testes

```bash
cd apps/desktop-pdv && python -m pytest -q
```

Nenhum teste precisa de balança, impressora ou rede: os protocolos e as regras
de preço são puros, e a balança de demonstração é simulada.

Os testes de tela **usam o Qt**, mas em modo `offscreen` — `tests/conftest.py`
força `QT_QPA_PLATFORM=offscreen`, então nenhuma janela abre e a suíte roda
igual numa build sem sessão gráfica. Eles existem porque a fiação da UI é uma
camada que teste de serviço não alcança: na Fase 3, um `from __future__ import
annotations` fez todas as rotas do servidor devolverem 422 com os serviços 100%
verdes por baixo.

---

## Atalhos de teclado do caixa

O operador não tira a mão do teclado numa fila.

| Tecla | Ação |
|---|---|
| `F2` | Registrar item pesado (só habilita com peso estável) |
| `F3` | Ir para a busca de item unitário (código ou nome) |
| `F4` | Cancelar item — exige credencial de gerente |
| `F6` | Desconto percentual — exige credencial de gerente, limitada ao teto do perfil |
| `F8` | Painel do salão: pareamento, mesas abertas e fila da cozinha |
| `F10` | Receber (dinheiro, débito, crédito, PIX ou dividido) e imprimir |

A autorização de gerente é validada **offline**, com Argon2id contra a réplica
local de `users.pin_hash`, e cada tentativa recusada vira evento de auditoria.
A base de demonstração traz `bruno` / `1234` como gerente (teto de 30%) e
`ana` / `1111` como caixa — que tem PIN válido e, de propósito, **não** pode
autorizar: liberar o próprio cancelamento é o furto inteiro em um passo.

---

## Avisos de integração com hardware

⚠️ **Protocolos de balança variam por firmware.** Os quadros implementados em
[`protocols.py`](apps/desktop-pdv/src/pdv/hardware/scale/protocols.py) cobrem as
variantes mais comuns no mercado brasileiro, mas número de dígitos, byte de
status e paridade mudam entre versões do mesmo modelo. Confira o manual do
equipamento e registre o quadro cru na homologação antes de fechar a
configuração da loja.

⚠️ **Impressão no Windows:** a rota recomendada é `win32print` em modo `RAW`,
que mantém o driver Epson instalado. A rota `python-escpos` via libusb exige
substituir o driver por WinUSB (Zadig), o que impede outros programas de usarem
a impressora.
