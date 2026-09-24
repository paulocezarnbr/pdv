# Contexto Fiscal — NFC-e server-first com contingência local

## Decisão

A emissão normal acontece no **servidor**, não no caixa. O Next.js autentica o
terminal, valida o tenant, reserva série/número no PostgreSQL e chama um serviço
fiscal Python privado. O certificado A1 e seu segredo ficam no cofre do servidor;
eles não atravessam a API pública.

O PDV Python conserva uma série própria somente para contingência. Ela só pode
ser usada quando a conexão com a nuvem comprovadamente nem chegou a ser
estabelecida. Timeout, HTTP 500 ou resposta ilegível são estados `unknown`: a
SEFAZ pode ter autorizado, então o terminal consulta o status e **não** emite
outro documento.

## Por que não uma biblioteca JavaScript agora

TypeScript é usado para autenticação, idempotência, numeração e orquestração —
onde ele é uma ótima escolha e já é a stack do backend. O motor fiscal fica sob
`FiscalProvider`, portanto pode ser trocado sem alterar PDV ou API.

O `@brasil-fiscal/nfe` foi avaliado, mas seu roadmap ainda marca contingência,
QR Code v3 e Reforma Tributária como pendentes e não demonstra uma matriz de
homologação para o RJ. O PyNFe 0.6.5 também mantém a adequação ao QR Code v3 em
aberto. Nenhuma das duas bibliotecas será declarada pronta para produção por
conveniência de linguagem.

O primeiro adaptador interno usa PyNFe para transporte/certificado e fica sob
uma **trava de homologação**. Produção só será habilitada após XML 4.00, QR Code
v3, regras da NT 2025.002 e cenários do RJ/SVRS passarem na suíte homologada.

## Componentes

1. `cloud-api` — autentica dispositivo, valida cadastro tributário, reserva a
   série normal e mantém o estado autoritativo.
2. `fiscal-service` — serviço FastAPI interno, protegido por token, com
   idempotência durável própria e acesso a referências de segredo montadas em
   diretório privado.
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

## Próxima fatia fiscal

- [x] Reconciliação automática dos documentos que nunca chegaram ao serviço.
- [ ] Consulta por chave na SEFAZ, para resolver `IN_FLIGHT` e `ENGINE_FAILURE`
      (depende do motor real).
- [ ] Gerador/assinador XML NFC-e 4.00 com QR Code v3 e validação XSD.
- [x] Cadastro fiscal em tela exclusiva do dono (`/api/panel/fiscal` e
      `/api/panel/fiscal/products`). Detalhes em "Cadastro fiscal pelo painel".
- [x] DANFE NFC-e 80 mm com indicação visível de contingência
      (`pdv/fiscal/danfe.py`). Imprime só o que veio do documento autorizado,
      nunca monta o QR Code, e **recusa** imprimir documento incoerente: chave
      com dígito verificador errado, de outro CNPJ ou modelo, série/número ou
      tipo de emissão divergentes da chave, emissão normal sem protocolo,
      contingência com protocolo, ou pagamentos que não fecham o total. Ainda
      não ligado ao caixa: depende do motor real extrair `qrCode` e `urlChave`
      do XML autorizado.
- [ ] Homologação formal RJ/SVRS antes de liberar `production` — exige o
      certificado A1 e o CSC da loja, emitidos pela SEFAZ-RJ.

SAT CF-e (modelo 59) permanece um adaptador separado: o hardware e o protocolo
não serão tratados como se fossem NFC-e.
