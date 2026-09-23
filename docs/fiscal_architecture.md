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

## Próxima fatia fiscal

- [x] Reconciliação automática dos documentos que nunca chegaram ao serviço.
- [ ] Consulta por chave na SEFAZ, para resolver `IN_FLIGHT` e `ENGINE_FAILURE`
      (depende do motor real).
- [ ] Gerador/assinador XML NFC-e 4.00 com QR Code v3 e validação XSD.
- [ ] Credenciais A1/CSC e cadastro tributário em tela exclusiva do dono.
- [ ] DANFE NFC-e 80 mm com indicação visível de contingência.
- [ ] Homologação formal RJ/SVRS antes de liberar `production` — exige o
      certificado A1 e o CSC da loja, emitidos pela SEFAZ-RJ.

SAT CF-e (modelo 59) permanece um adaptador separado: o hardware e o protocolo
não serão tratados como se fossem NFC-e.
