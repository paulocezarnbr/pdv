# Passagem de bastão — o que falta para terminar o PDV em C#

Escrito em 25/09/2026 para continuar o porte em outra instância. O histórico
completo de cada fase está em `docs/port_csharp.md`; as pendências de
produto estão em `docs/plan.md`.

## Onde está

- **Repositório:** `paulocezarnbr/pdv`.
- **Branch de trabalho:** `port/csharp-winui`, em `0e197e7` (C6c).
- **`main`:** em `57b4ca4`, que inclui C3c, C5b-3, C6a e C6b, com o CI verde
  nos cinco jobs. Só se publica na `main` com o ok explícito do dono, e
  cada publicação é autorizada à parte.
- **C6c (`0e197e7`):** está na branch. O CI dela ainda não foi conferido;
  confira antes de pedir a publicação.

**Pronto em C#:**

| Área | O que já funciona |
|---|---|
| Base | Auditoria, venda com TEF (simulado), login por PIN com freio, cofre, ativação e sincronização |
| Itens | Item por unidade e por peso (balança serial) |
| Operações com autorização | Cancelamento e desconto, com a autorização |
| Painel | Comandos do painel com as sete travas |
| Caixa | Abertura e fechamento cego |
| Cliente | Cashback, pré-pago, fiado e níveis |
| Impressão | Cupom e DANFE byte a byte, impressora e fila |

O serviço fiscal inteiro (`apps/fiscal-net`) também já é C#. São 445 testes
em `apps/pdv-net`.

## Como se trabalha aqui (não quebrar)

- **O Python é o gabarito.** Cada regra portada é conferida contra um
  contrato em `contracts/*.json`, gerado por um teste em
  `apps/desktop-pdv/tests/test_*_contract.py`. Para regerar:

  ```
  PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_x_contract.py
  ```

  O teste C# lê o JSON e exige igualdade.
- **Regra de segurança nova passa por mutação.** Um script troca a regra,
  roda `dotnet test` e espera falha. Mutante que sobrevive é lacuna de teste,
  e se corrige no teste.
- **Divergência do Python só de propósito.** Fica documentada como "regra a
  mais que o Python" no código e no `port_csharp.md`. O que o Python faz de
  errado e ainda não foi corrigido fica em `plan.md`, sem mudança em
  silêncio.
- **Tela.** Sem janela, a tela se testa pelo `Pdv.App` (view models). O
  `apps/pdv-net/tools/ui-smoke.ps1` dirige o `PDV.exe` real por UI
  Automation. Ele só roda no Windows (PowerShell 5.1, arquivo com BOM).
- **Linux.** `Pdv.Core` (net10.0), `apps/cloud-api` e `apps/fiscal-net`
  compilam e testam no Linux. `Pdv.Data`, `Pdv.App`, `Pdv.WinUI` e os testes
  são `net10.0-windows`. No Linux, compile com
  `-p:EnableWindowsTargeting=true`. O que usa DPAPI, winspool e WinUI só se
  prova no job "PDV em C# (Windows, .NET 10)" do CI.
- **Commits.** Mensagem em português e o rodapé
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **CI.** O `cancel-in-progress` está ligado: um push novo cancela a
  execução anterior da branch. A API anônima do GitHub estoura o limite
  rápido, então use `gh` autenticado ou a página de Actions.
- **Segredos.** O A1, a senha dele e o CSC nunca passam pela API. Segredo
  gerado não se imprime nem se grava. A chave de API do Coolify é só de
  deploy e nunca vai para o repositório.

## O que falta, na ordem

### 1. C6d — NFC-e pedida pelo caixa (online) — **FEITA em 26/09/2026**

Ver `docs/port_csharp.md`. Só a contingência offline (item 6 abaixo) segue
esperando a decisão do dono. O texto original fica como registro:

Nem o Python faz isso pela tela: `pdv/fiscal/cloud.py` só é usado em teste.
A retaguarda já tem `POST /api/fiscal/issue` e `GET /api/fiscal/status`
(`apps/cloud-api/src/app/api/fiscal/*`).

1. **Retaguarda.** Devolver ao terminal o XML autorizado (`processed_xml`),
   que já é gravado em `fiscal_documents.xml_content` mas não sai em
   `out()`, em `src/lib/fiscal/service.ts`. Só na rota do dispositivo, com
   teste de integração.
2. **Leitura do XML no PDV.** Ler o `nfeProc` em `NfceDanfe`
   (`Pdv.Core.Printing.NfceDanfeLayout` já existe e valida). Os campos:
   - emitente, `det`, `pag` (`tPag` → forma, o inverso do mapa do
     `fiscal-net`) e `vTroco`;
   - `infNFeSupl/qrCode` e `urlChave`, `protNFe` (`nProt`, `dhRecbto`),
     `ide` (série, número, `dhEmi`, `tpAmb`, `tpEmis`);
   - `vDesc`, `vTotTrib` e `dest`.

   O DANFE **não calcula nem monta** nada: imprime o que veio do XML.
3. **Cliente HTTP.** A classificação é a do `cloud.py`:

   | Resposta | Tratamento |
   |---|---|
   | Sem conexão TCP | "offline certo" |
   | Timeout, 5xx ou resposta ilegível | "resultado desconhecido": nunca emitir outra |
   | 401/403 | Autenticação |
   | 4xx | Recusa |

   O `request_uuid` é estável por pedido, e total zero dá `not_required`.
4. **Fluxo.** Fecha a venda, empurra o outbox (o pedido precisa existir na
   nuvem) e pede a nota com prazo curto:
   - autorizada: imprime o DANFE no lugar do cupom;
   - senão: imprime o cupom e um trabalhador em segundo plano consulta o
     status, com o DANFE em reimpressão depois.
5. **Chave de configuração.** Tudo atrás de uma chave em `device_settings`
   (ex.: `fiscal.enabled`), **desligada** até a homologação na SEFAZ-RJ.
6. **Contingência offline** (`tpEmis 9`, série própria, QR v3 assinado).
   Exige o A1 **em cada terminal**. **Não construir sem a decisão do dono**
   (ver Decisões pendentes).

### 2. C6e — Servidor do salão (a maior) — **em andamento**

Feito em 26/09/2026 (detalhe em `docs/port_csharp.md`): mesas, comandas,
cozinha, barramento, pareamento, sessão do garçom, gerente e resultado do
turno, conferidos contra `contracts/salon.json`; o servidor HTTP e o
WebSocket da cozinha (`Pdv.Edge`), contra `contracts/salon-http.json`. Falta
TLS, anúncio na rede e a ligação no caixa. No caminho,
corrigido nos dois PDVs: item de mesa com ficha técnica não baixava insumo.

`apps/desktop-pdv/src/pdv/edge/` (~4.000 linhas) em ASP.NET Core dentro do
PDV:
- mesas, comandas e contas;
- KDS (tela da cozinha) e o hub de eventos;
- pareamento de aparelhos e descoberta na rede;
- TLS.

O app web do garçom (`edge/webapp`) é reaproveitado como está, então a API
tem de ser a mesma. Gere contratos das rotas pelo Python antes de portar.
Tabelas `kds_tickets`, `store_tables` e afins já estão no schema.

### 3. C6f — Telas que faltam

Painel do salão, mesas e recebimento da conta da mesa, relatório da equipe
(`services/staff_report.py`) e F1 com todos os atalhos (`ui/salon_panel.py`,
`ui/tables_dialog.py`, `ui/dialogs.py`, `ui/remote_dialog.py`).

### 4. C7a — Migrations em C#

Hoje o banco nasce do `migrate()` do Python (`data/database.py`), e o C# só
abre a versão 14. O C# precisa:
- criar o banco;
- atualizar das versões antigas;
- carregar a demonstração (`data/seed.py`).

Confira contra `contracts/pdv-schema.sql`.

### 5. C7b — Detecção e autoteste

`provisioning/detection.py` (acha balança e impressora e grava
`device_settings`), `selftest.py` e `smoke.py`.

### 6. C7c — Instalador

Passa a entregar o `PDV.exe` .NET self-contained. Mantém reparo, atualização
com detecção de versão e o `harden.ps1` (`setup_wizard.py`, `main.py`,
`packaging/`).

### 7. C7d e C7e — Contratos congelados e ponta a ponta em C#

- **C7d.** Os JSON de `contracts/` viram arquivos fixos, sem gerador Python,
  e o `apps/pdv-net/crosscheck.py` sai.
- **C7e.** O ponta a ponta do CI (`apps/cloud-api/scripts/e2e.py`,
  `e2e_terminal.py`) passa ao `apps/pdv-net/tools/CloudE2E`. Inclui o comando
  remoto emitido pelo painel (login com senha e Turnstile).

### 8. Remover o Python

Sai `apps/desktop-pdv` inteiro e o job "PDV (Windows, Python)". Só depois de
1 a 7 no ar e de uma loja ter rodado o PDV em C#.

## Correções anotadas (fazer no caminho)

- **Fiado recebido fora da gaveta.** O recebimento do fiado no balcão não
  entra no esperado do fechamento cego (`plan.md`). Registrar o recebimento
  com forma de pagamento. Vale nos dois PDVs.
- **CNPJ e endereço da loja.** A ativação não grava `store.document` e
  `store.address`, e o cupom sai com os marcadores fictícios do Python. A
  retaguarda tem o CNPJ no cadastro fiscal e deve mandá-lo na ativação.
- **Mensagens que revelam arquitetura interna.** Nomes de variável de
  ambiente, `/api/health` detalhado e caminhos nas mensagens do PDV
  (`plan.md`).
- **Formas de pagamento editáveis pelo painel.** Desenho em `plan.md`.

## Decisões pendentes do dono (perguntar, não assumir)

1. **Contingência NFC-e offline.** Aceitar o A1 instalado em cada caixa, ou
   sem internet sai só o cupom e a nota depois?
2. **Cashback como desconto ou pagamento** (decisão do contador). O resgate
   só é ligado no caixa depois dela.
3. **Provedor de TEF.** Hoje é simulador (`ITefProvider` + `TefSimulator`).

## Fora do código (com o dono)

- Homologação da NFC-e na SEFAZ-RJ com o A1 real.
- No Coolify, publicar o `apps/fiscal-net` (cofre, volumes, variáveis) e
  as chaves do Turnstile.
- Ativar o primeiro caixa com um código gerado no painel.
