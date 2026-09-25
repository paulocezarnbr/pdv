# Contexto Fiscal — NFC-e server-first com contingência local

## Decisão

A emissão normal acontece no **servidor**, não no caixa. O Next.js autentica o
terminal, valida o tenant, reserva série/número no PostgreSQL e chama o serviço
fiscal privado (`apps/fiscal-net`, C# com DFe.NET). O certificado A1 e seu
segredo ficam no cofre do servidor; eles não atravessam a API pública.

O PDV Python conserva uma série própria somente para contingência. Ela só pode
ser usada quando a conexão com a nuvem comprovadamente nem chegou a ser
estabelecida. Timeout, HTTP 500 ou resposta ilegível são estados `unknown`: a
SEFAZ pode ter autorizado, então o terminal consulta o status e **não** emite
outro documento.

## O motor: C# com DFe.NET

TypeScript fica com autenticação, idempotência, numeração e orquestração. O
motor fiscal fica atrás de `FiscalProvider` (`HttpFiscalProvider`, pela rede do
Coolify), então pode ser trocado sem mexer no PDV nem na API.

O serviço em Python (FastAPI + PyNFe) nunca passou da trava: o PyNFe não tinha
QR Code v3 nem IBS/CBS. O serviço atual é C#/.NET 10 com o **DFe.NET (Zeus)**,
LGPL-2.1, usado como pacote e sem modificação. A escolha foi comparada com o
Unimake.DFe (MIT) e decidida por prova em contêiner Linux, não por README:

| | DFe.NET | Unimake.DFe |
|---|---|---|
| Linux | `net6.0` declarado para Linux. A NFC-e foi montada, assinada, validada no XSD e transmitida ao SVRS **dentro do contêiner** | depende de `WinHttpHandler`, que só existe no Windows |
| Configuração por chamada | sim: certificado e config por loja, sem o singleton global | — |
| QR Code | v1, v2 e v3 | — |
| IBS/CBS (NT 2025.002) | classes e XSDs presentes | declarado |

**QR Code v3 por padrão (NT 2025.001).** Na emissão normal ele é
`chave|3|ambiente`: sem CSC e sem hash. O CSC deixa de ser segredo obrigatório
da loja. Desde 1º de setembro de 2025 as SEFAZ aceitam o v3, e o v2 continua
aceito. `FISCAL_QRCODE_VERSION=2` volta ao v2 com CSC, se alguma SEFAZ recusar.
O v3 assinado com o A1 é o da **contingência**, que é do PDV, não deste serviço.

**Produção** continua atrás de duas travas independentes, a da retaguarda e a
do serviço. Cada uma exige `FISCAL_PRODUCTION_ENABLED=true`, e só se abrem
depois da homologação com o A1 e a inscrição reais da loja.

## Componentes

1. `cloud-api` — autentica dispositivo, valida cadastro tributário, reserva a
   série normal e mantém o estado autoritativo.
2. `fiscal-service` (`apps/fiscal-net`) — serviço ASP.NET Core interno,
   protegido por token. Tem idempotência durável própria (SQLite, a mesma
   tabela do serviço anterior) e lê as referências de segredo numa pasta
   privada montada no contêiner.
3. `desktop-pdv` — cliente da API e emissor local exclusivamente em contingência.

## Invariantes

1. Série normal pertence à loja e é numerada centralmente.
2. Cada terminal tem uma série de contingência diferente.
3. `request_uuid` identifica a tentativa de ponta a ponta; repeti-la não chama
   o provedor nem consome outro número.
4. `processing` ou `unknown` nunca autoriza emissão local substituta.
5. Documento e evento fiscal não são apagados; correções são eventos novos.
6. `authorized` exige `cStat` fiscal aceito, chave, protocolo e XML processado.
7. Falta de NCM/CFOP/CSOSN/CST ou cadastro do emitente recusa antes de reservar;
   o sistema não inventa tributação.
8. O A1 é referenciado por nome de segredo. Bytes e senha não aparecem em
   payload, PostgreSQL, log ou resposta ao terminal.

## Estados

`processing → authorized | rejected | unknown`

`unknown → consulta por chave → authorized | rejected`

`contingency_pending → transmissão posterior → authorized | rejected`

`authorized → evento de cancelamento → canceled`

## Reconciliação: os dois `unknown`

O serviço fiscal responde `unknown` em dois casos com consequências opostas, e
por isso eles têm códigos diferentes:

| Código | O que aconteceu | O que a nuvem faz |
|---|---|---|
| `NOT_FOUND` | A chamada de autorização **nunca chegou** ao serviço. O motor não rodou; a SEFAZ não viu nada. | Retransmite com o **mesmo** número e o **mesmo** `request_uuid`, depois de 10 s. |
| `IN_FLIGHT` | O serviço reivindicou a solicitação e não concluiu. O motor pode ter transmitido. | **Não** retransmite. Fica `unknown` até a consulta por chave na SEFAZ. |
| `ENGINE_FAILURE` | O motor lançou exceção depois de iniciar. | Idem `IN_FLIGHT`. |

Antes os dois primeiros eram o mesmo código (`NOT_SETTLED`), e o documento cujo
processo caiu entre reservar o número e transmitir ficava `processing` para
sempre. A retransmissão é segura por construção: o `claim` do serviço fiscal é a
única porta do motor, então uma chamada original atrasada na rede e a
retransmissão disputam o mesmo `request_uuid` e o motor roda uma vez.

A retransmissão passa pelas **mesmas** validações da primeira emissão
(`readEmissionInputs`), inclusive a trava de produção.

**Restrição operacional:** o estado do serviço fiscal é um SQLite num volume.
Ele precisa rodar com **uma réplica só** — duas réplicas com volumes separados
não compartilhariam o `claim`, e a garantia acima deixaria de valer.

## Travas antes da reserva

A nuvem reserva o número **antes** de chamar o serviço fiscal. Por isso duas
checagens acontecem antes da reserva, e não depois:

1. **Serviço fiscal configurado.** Sem `FISCAL_SERVICE_URL`/`TOKEN`, a emissão
   responde 503 e nenhum número é consumido. A retaguarda continua saudável — a
   sincronização não depende de NFC-e.
2. **Produção liberada.** Com `environment='production'` e sem
   `FISCAL_PRODUCTION_ENABLED=true`, a emissão é recusada com 409. Sem esta
   trava, o motor (travado em homologação) rejeitaria cada venda depois de ela
   já ter consumido um número real da série, e cada buraco exigiria
   inutilização formal na SEFAZ.

## Venda com total zero não emite nota

Desconto de 100% (cortesia, degustação, consumo da equipe) ou produto de preço
zero fecham a venda com total R$ 0,00, e **não geram NFC-e**. Uma nota de valor
zero não tem o que tributar e seria rejeitada pela SEFAZ depois de já ter
consumido um número da série — que então exigiria inutilização formal.

O desfecho é `not_required`: não é erro nem estado pendente, e ninguém tenta de
novo. A regra vale nos três lugares onde uma nota pode nascer:

| Onde | Como |
|---|---|
| Nuvem (`POST /api/fiscal/issue`) | Responde `200 {"status":"not_required"}` sem criar documento nem tocar na série. |
| Terminal (`FiscalCoordinator`) | Pergunta `requires_document` **antes** de falar com a nuvem — uma cortesia feita offline não pode cair no ramo de contingência. |
| Contingência (`FiscalService.reserve`) | Levanta `FiscalNotRequired` antes de ler a série, mesmo que alguém a chame direto. |

A ordem das checagens na nuvem é: *a venda precisa de nota?* → *o serviço
fiscal está configurado?* → *reserva*. Assim a cortesia recebe "não precisa de
nota" mesmo com o fiscal desligado, em vez de um 503 que o caixa mostraria como
erro.

- A regra é sobre o total **zero**, não sobre ter desconto: 99% de desconto
  ainda é venda com valor e ainda emite, pelo valor com desconto.
- Total **negativo** é defeito, não cortesia: recusado com erro (409 na nuvem,
  `FiscalError` no terminal), nunca silenciado.
- A trilha não se perde: o desconto de 100% exige autorização de gerente/dono
  e fica no ledger de auditoria. É ali — e não numa nota de R$ 0,00 — que se
  confere quem liberou a cortesia.

## Cadastro fiscal pelo painel

Só o **dono** lê e altera. O cadastro decide em nome de qual CNPJ as notas
saem, e a responsabilidade tributária é dele: gerente recebe 403 nas duas
rotas.

- **O certificado, a senha e o CSC nunca passam por esta API.** O painel guarda
  apenas o *nome* da referência no cofre do serviço fiscal
  (`loja-centro/a1.pfx`). Um corpo com campo que pareça segredo (`senha`,
  `password`, `pfx`, `p12`, `pem`, `base64`…) é recusado com 422 **antes** de
  qualquer validação, e nada é gravado. Referências absolutas ou com `..` são
  recusadas, com as mesmas regras do `SecretResolver` do serviço fiscal.
- **Erro de cadastro é acusado no cadastro, não na venda.** O CNPJ tem os
  dígitos verificadores conferidos; o município precisa ter o prefixo IBGE da
  UF; `ISENTO` não serve de inscrição estadual para emitente de NFC-e.
- **O perfil do produto é do contador; o sistema confere, não sugere.** NCM
  com 8 dígitos, CFOP de operação interna (5xxx: NFC-e é venda dentro do
  estado), CEST com 7 dígitos quando houver, e **exatamente um** entre CSOSN e
  CST de ICMS, coerente com o regime das lojas: Simples e MEI usam CSOSN;
  Regime Normal usa CST. Um tenant com lojas em regimes diferentes aceita os
  dois.
- **Emissão desligada salva parcial.** Dá para preencher o emitente antes de ter
  o A1. Para **ligar** a emissão, as três referências (certificado, CSC e ID do
  CSC) passam a ser obrigatórias. Ligar a emissão e mudar para produção pedem
  confirmação explícita na tela.
- **Produção continua travada** enquanto `FISCAL_PRODUCTION_ENABLED` não for
  `true`: a tela recusa o cadastro em vez de aceitar e deixar a primeira venda
  falhar.
- **A série não muda depois de emitir.** Trocar a série normal de uma loja que
  já tem documento responde 409.
- **Tudo é auditado.** Cada alteração grava `fiscal_config_updated` ou
  `fiscal_profile_updated` em `panel_admin_events`, que a aplicação pode
  inserir mas não alterar.
- A tela lista **o que ainda impede a primeira nota**: cadastro ausente, série,
  emissão desligada, produtos sem perfil completo e serviço fiscal não
  configurado na retaguarda.

## O emissor em C#: o que ele faz

**Segredos no cofre** (`FISCAL_SECRETS_DIR`, pasta montada só-leitura):

| Arquivo | Conteúdo |
|---|---|
| `<certificateRef>` | o A1 (`.pfx`), por exemplo `loja-centro/a1.pfx` |
| `<certificateRef>.senha` | a senha dele (sem o arquivo: A1 sem senha) |
| `<cscRef>` | o CSC, só com `FISCAL_QRCODE_VERSION=2` |

**Antes de reivindicar o pedido, nada é consumido.** O serviço confere a trava
de produção e se o A1 abre e está na validade. Um problema aqui volta como
`unknown`/`FISCAL_SETUP` com o motivo, sem gravar nada.

**Depois de assinado, o XML e a chave são gravados antes de transmitir.** É o
que permite a reconciliação por chave, antes pendente:

| Resposta da SEFAZ | Desfecho |
|---|---|
| cStat 100 ou 150 | `authorized`, com o `nfeProc`: o XML assinado intacto mais o `protNFe` como a SEFAZ o mandou |
| Recusa (cStat da nota ou do lote) | `rejected`, com o cStat e o motivo dela |
| 204/539 (duplicidade) | consulta pela chave. Nunca se presume autorizada |
| 103, 105, 108, 109, 656, 999 | `unknown`, em aberto: a SEFAZ está ocupada ou fora |
| Sem resposta (rede, timeout) | `unknown`/`SEFAZ_UNREACHABLE`, em aberto |
| HTTP 401/403 na porta (certificado barrado no TLS) | O XML é **descartado**: nada foi processado. A retransmissão assina de novo, com o A1 corrigido, e o número não se perde |

**A consulta (`/v1/fiscal/status`) resolve o que ficou em aberto:**

- **Chave autorizada:** `authorized`.
- **217 (não consta):** retransmite **o mesmo XML**. Se a primeira tentativa
  aparecer, é duplicidade da mesma chave, nunca nota dobrada.
- **Denegada:** `rejected`.

Um teste prova que o XML recarregado sai idêntico byte a byte e que a
assinatura continua valendo dentro do `nfeProc`, validado no XSD oficial.

**Tributação: o emissor não inventa.** Emite:

- **Simples:** CSOSN 102, 103, 300, 400 e 500.
- **Regime normal sem destaque:** CST 40, 41, 50 e 60.
- **PIS/COFINS:** 04 a 09 e 49/99.

O resto (CSOSN 101 e 201+, CST 00/10/20, PIS/COFINS 01/02) precisa de
alíquota que o cadastro ainda não guarda. A retaguarda recusa **antes de
reservar**, com o nome do produto, e o painel lista esses produtos entre os
impedimentos da primeira nota.

**Pagamento.** A NFC-e exige o grupo `pag`, e a retaguarda manda os pagamentos
da venda:

| PDV | tPag |
|---|---|
| `cash` | 01 |
| `credit` / `debit` | 03 / 04, com o grupo `card` "não integrado" até o provedor de TEF ser escolhido |
| `pix` | 17 |
| `prepaid` | 21 (crédito em loja) |
| `credit_account` | 05 (crediário) |
| `cashback` | 19 (fidelidade/cashback) |

O troco vai em `vTroco`. Pagamento que não fecha o total é recusado antes de
reservar.

**Valores.**

- `vProd` bate com `qCom × vUnCom` em até um centavo (regra 629).
- O desconto do item fica no item.
- O desconto do pedido é rateado pelos itens pelo maior resto, sem sobrar
  centavo.
- Acréscimo não é emitido.

**Reforma Tributária.** O grupo IBS/CBS não é enviado ainda. Em 2026 ele é
dispensado para o Simples e informativo para os demais. Os XSDs e as classes
já o têm; ele entra quando o produto tiver a classificação no cadastro.

## Próxima fatia fiscal

- [x] Reconciliação automática dos documentos que nunca chegaram ao serviço.
- [x] Consulta por chave na SEFAZ, para resolver `IN_FLIGHT` e falhas depois
      da transmissão.
- [x] Gerador e assinador de XML NFC-e 4.00, QR Code v3 e validação no XSD
      (`apps/fiscal-net`).
- [x] Cadastro fiscal em tela exclusiva do dono (`/api/panel/fiscal` e
      `/api/panel/fiscal/products`). Detalhes em "Cadastro fiscal pelo painel".
- [x] DANFE NFC-e 80 mm com indicação visível de contingência
      (`pdv/fiscal/danfe.py`). Imprime só o que veio do documento autorizado,
      nunca monta o QR Code, e **recusa** imprimir documento incoerente. Ainda
      não ligado ao caixa: o `nfeProc` autorizado já traz `qrCode` e
      `urlChave`, e falta o PDV em C# ler e imprimir.
- [ ] Homologação formal RJ/SVRS antes de liberar `production`. Exige o
      certificado A1 e a inscrição estadual reais da loja. A prova no
      contêiner chegou até o TLS do SVRS, que recusou o certificado de teste
      (403), como deve.
- [ ] Alíquotas no cadastro (CSOSN 101, CST 00/20, PIS/COFINS 01/02) e o grupo
      IBS/CBS.
- [ ] Cancelamento (evento 110111) e inutilização de numeração.
- [ ] `tpIntegra = 1` com CNPJ da credenciadora e autorização, quando o
      provedor de TEF for escolhido.

SAT CF-e (modelo 59) permanece um adaptador separado: o hardware e o protocolo
não serão tratados como se fossem NFC-e.
