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

✅ **Fases 1, 2, 2.5, 3, 3.5, 3.6 e 4** implementadas; a fundação da Fase 5
(fiscal) já começou. A suíte desktop tem **457 testes** passando.

O que já funciona ponta a ponta, sem internet:

1. Leitura da balança via porta serial (Toledo Prix 3 · Filizola · Urano), com
   detecção de peso estável em thread separada.
2. Cálculo de preço por peso com tara descontada e `ROUND_HALF_UP` em `Decimal`,
   além de venda por unidade (café, fatia, bebida).
3. **Baixa fracionada** de insumos pela ficha técnica, em miligramas inteiros,
   com fator de rendimento e percentual de perda.
4. Ledger de auditoria **imutável encadeado por HMAC-SHA256** (anti-furto), com
   cancelamento e desconto exigindo credencial de gerente validada offline.
5. Impressão **ESC/POS bruta** para Epson TM-T20X (80 mm) com guilhotina e
   acionamento de gaveta.
6. Recebimento com dinheiro, débito, crédito, PIX ou pagamento dividido.
7. Tudo isso numa **única transação SQLite** + enfileiramento no outbox de sync.
8. **Servidor local do salão** (`_pdvedge._tcp`): o PDV é o servidor do app do
   garçom e do KDS, com idempotência ponta a ponta por `client_uuid`.
9. **App do garçom em web**, servido pelo próprio PDV — mapa de mesas, comanda,
   pedido de conta e opções de gerente. Ver a seção abaixo.
10. **Comandos remotos do painel**, ponta a ponta: desconto e cancelamento
    vindos da nuvem são aplicados **uma vez só**, dentro dos tetos do perfil de
    quem emitiu, e o resultado volta para o painel com o motivo da recusa.
11. Instalador único com provisionamento automático de periféricos, ativação do
    terminal e atualização in-place.
12. Painel web com cadastro de múltiplos proprietários: somente outro dono
    autenticado pode criar a conta, o PIN é Argon2id e cada criação entra numa
    auditoria administrativa imutável.
13. Fiscal **server-first**: a nuvem reserva a série normal e um serviço Python
    interno isola o certificado/provedor; o PDV usa série própria apenas em
    queda comprovada antes do envio. Timeout ambíguo bloqueia uma segunda NFC-e.

🔜 Próximo: homologar XML NFC-e 4.00, QR Code v3 e NT 2025.002 no RJ/SVRS. O
motor de produção fica deliberadamente bloqueado até essa suíte passar; o núcleo
não declara uma nota “autorizada” antes da resposta fiscal real.

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

### HTTPS nos celulares do salão

O certificado local protege PINs e tokens contra captura na rede, mas a CA da
loja precisa ser confiada uma vez em cada aparelho. Confiar no Windows não
confia automaticamente nos celulares. No iPhone, instale o perfil e habilite
"Confiança total" em Ajustes; em frota administrada, prefira MDM/Apple
Configurator. No Android, instale a CA da loja no armazenamento de credenciais.
O futuro app nativo usará pinning do certificado do terminal; a versão web não
contorna avisos TLS, pois ensinar o garçom a ignorá-los anularia a autenticação.

| Tecla | Ação |
|---|---|
| `F2` | Registrar item pesado (só habilita com peso estável) |
| `F3` | Ir para a busca de item unitário (código ou nome) |
| `F4` | Cancelar item — exige credencial de gerente |
| `F5` | Configurar limite ou receber Fiado/Pendura |
| `F6` | Desconto percentual — exige credencial de gerente, limitada ao teto do perfil |
| `Ctrl+F6` | Configurar/atribuir níveis Bronze, Prata, Ouro, Diamante, Funcionário e Dono |
| `F7` | Configurar cashback (percentual, teto e validade) — exige gerente |
| `F8` | Painel do salão: pareamento, mesas abertas e fila da cozinha |
| `F10` | Receber (dinheiro, débito, crédito, PIX ou dividido) e imprimir |
| `F11` | Carregar crédito pré-pago de cliente — exige gerente |
| `F12` | Fechamento cego do caixa — exige credencial de gerente |

Os níveis padrão começam em Bronze 2%, Prata 4%, Ouro 6%, Diamante 10%,
Funcionário 15% e Dono 20%; todos os percentuais são configuráveis. O nível
**Dono** é uma exceção de segurança: sempre exige login e PIN de um usuário com
papel de proprietário a cada aplicação. Somente um proprietário pode atribuir
os níveis Funcionário ou Dono. Essas travas são validadas no serviço, inclusive
para dados recebidos por sincronização. Funcionário e Dono são classificações
permanentes: depois de atribuídas, não podem ser convertidas em outro nível.

A autorização de gerente é validada **offline**, com Argon2id contra a réplica
local de `users.pin_hash`, e cada tentativa recusada vira evento de auditoria.
A base de demonstração traz `olivia` / `84627519` como proprietária (teto de
100%), `bruno` / `483916` como gerente (teto de 30%) e `ana` / `705284` como
caixa. Cancelar item exige especificamente o gerente; atribuir Funcionário/Dono
e usar o nível Dono exigem especificamente a proprietária.

O tenant pode ter **mais de um proprietário**. No painel web, uma sessão com
papel `owner` abre “Adicionar outro dono”; gerente não acessa a rota. O servidor
valida a mesma política de PIN do PDV, grava somente Argon2id e a réplica chega
aos caixas pelo sync de `users`. Não há remoção de proprietário nesta etapa,
evitando exclusão acidental do último dono.

---

## App do garçom

O PDV serve o app do salão em `http://<ip-do-caixa>:8420`. O endereço aparece
no **Painel do salão** (F8), junto do código de pareamento: o garçom abre no
navegador do celular, digita o código e está dentro. Dá para fixar na tela
inicial — abre em tela cheia, sem barra de endereço.

| Tela | O que faz |
|---|---|
| Mapa do salão | Mesas por área, com livre / ocupada / **pedindo a conta** e o total |
| Comanda | Itens com o estado na cozinha, lançar item, pedir a conta |
| Gerente (⚙) | Configurar mesas, transferir comanda, cancelar comanda |

Decisões que valem registro:

| Decisão | Motivo |
|---|---|
| Web, servido pelo PDV | Sem loja de aplicativo e sem versão de celular defasada falando com um terminal novo — a classe de bug mais cara de diagnosticar por telefone. |
| **Não** é offline-first | O PDV é a autoridade da comanda. Guardar pedido no celular criaria uma segunda fonte de verdade sobre o que a mesa consumiu. Wi-Fi caído tem solução física; conta divergente, não. |
| Uma comanda por mesa | Dois garçons abrindo a mesma mesa partiam a conta em duas que ninguém junta na hora de cobrar. Tocar em mesa ocupada abre a comanda que já existe. |
| Pedir a conta ≠ receber | Um segundo ponto de recebimento, sem gaveta e sem conferência de troco, é como o furto de salão entra pela porta da frente. |
| Gerente vale 10 min, por aparelho | O celular fica no balcão desbloqueado a noite inteira. Sessão que durasse o turno seria promover o aparelho. |
| Item por peso ausente do cardápio | Quem pesa é a balança do balcão, que guarda o quadro cru como prova. Peso digitado por quem cobra é o buraco que o módulo anti-furto existe para fechar. |
| Mesa desativa, nunca apaga | Comandas antigas apontam para ela; apagar a linha custaria todo o relatório de faturamento por mesa. |

O PIN de gerente é validado **offline**, com o mesmo Argon2id e o mesmo
bloqueio progressivo do balcão. Na base de demonstração, use `bruno` / `483916`
para gerente; a senha de proprietário é `olivia` / `84627519`.

---

## Sistema visual

Tudo que a interface desenha sai de [`ui/theme.py`](apps/desktop-pdv/src/pdv/ui/theme.py):
cor, escala tipográfica, espaçamento, raio e a folha de estilo. Nenhum widget
escolhe hexadecimal por conta própria.

**O tema é fixado, não herdado.** `apply_theme` chama `setStyle("Fusion")` e
instala uma paleta explícita. Sem isso o Qt segue o tema do Windows: o mesmo
PDV ficaria escuro numa máquina e claro na outra, e o contraste calculado para
ler o peso de pé, a um metro do balcão, sob lâmpada fria, valeria só na máquina
que foi testada.

Decisões que valem registro:

| Decisão | Motivo |
|---|---|
| Um único acento (azul-aço) | Verde, âmbar e vermelho já são os estados da balança. Acento que também é status faz botão parecer aviso. |
| Preto que não é `#000000` | Preto absoluto num monitor de balcão espelha a luminária do teto. |
| Algarismos tabulares (`tnum`) em tudo | Coluna de dinheiro em fonte proporcional não alinha na vírgula: conferir a venda vira leitura dígito a dígito. |
| Peso em corte *display*, não monoespaçada | `tnum` já impede o número de dançar enquanto a balança oscila; a monoespaçada daria o mesmo e ainda abriria um vão do tamanho de um dígito em volta da vírgula. |
| `Segoe UI Variable` com cadeia de fallback | Pesos intermediários reais e eixo óptico, sem embarcar fonte no instalador. Em Windows 10 a cadeia cai para `Segoe UI`. |
| Estado vazio desenhado | A tela sem itens é o estado mais frequente do dia. Retângulo em branco com cabeçalho de coluna não diz "está tudo certo", diz "algo não carregou". |

Três recomendações comuns de design de web foram **recusadas** de propósito,
porque um PDV não é uma landing page:

* **Macro-whitespace.** Dobrar o respiro custa linhas visíveis da venda.
* **Animação de entrada e rolagem.** Meio segundo de transição entre "peso
  estável" e "item registrado" é tempo em que o operador não sabe se pode
  tirar a mercadoria da balança. Só há movimento em resposta a toque.
* **Assimetria e grid quebrado.** O olho do operador precisa cair no mesmo
  lugar milhares de vezes por dia.

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
