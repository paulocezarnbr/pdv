# Contexto Fiscal — NFC-e offline-first

## Limite desta fase

O núcleo fiscal não confunde **reservar** com **autorizar**. O PDV já consegue
reservar série/número e registrar uma emissão normal ou em contingência, mas só
um adaptador estadual, falando com a SEFAZ e usando certificado válido, poderá
mover o documento para `authorized`.

## Provedor escolhido para o Rio de Janeiro

O adaptador inicial usa **PyNFe 0.6.5** (LGPL-3.0), pinado no instalador. É
nativo Python e preserva o funcionamento local do PDV; adicionar um serviço
PHP ou um SaaS entre o caixa e a SEFAZ criaria mais um ponto de falha e outra
credencial de longa duração. A SEFAZ-RJ utiliza a SVRS para NFC-e, e um teste de
contrato fixa a URL de homologação resolvida pela biblioteca.

O domínio conhece apenas `FiscalGateway`. Migrar futuramente para uma API
externa exige outro adaptador, sem alterar série, estados ou idempotência.
PyNFe não decide que uma nota foi autorizada: o adaptador só aceita `cStat`
100/150 e conserva XML processado, chave e protocolo.

O pacote publicado não declara dependências no metadata do wheel 0.6.5. Por
isso `signxml`, `lxml`, `pyOpenSSL` e `requests` estão explicitamente pinados em
`requirements.txt`; confiar na instalação transitiva faria o executável falhar
somente na primeira emissão.

O instalador inclui o metadata, autores e a licença LGPL do PyNFe. A licença
permite o aplicativo comercial fechado, mas os avisos e os direitos sobre a
biblioteca aberta não são removidos nem disfarçados.

## Invariantes

1. Série pertence a `(tenant, loja, terminal, modelo)`. Duas estações nunca
   compartilham contador.
2. Uma venda e um modelo têm no máximo um documento. Repetir após timeout
   devolve a primeira reserva.
3. Série usada não pode ser trocada. Alteração exigirá encerramento formal e
   nova configuração, não um `UPDATE` silencioso.
4. Documento fiscal não é apagado. Correções são novos eventos fiscais.
5. `contingency_pending` não significa “autorizado”; significa que a venda foi
   emitida localmente e ainda precisa de transmissão posterior.
6. Número reservado não pode ser entregue a outra venda.

## Estados

`pending` → `authorized | rejected`

`contingency_pending` → transmissão posterior → `authorized | rejected`

`authorized` → evento fiscal de cancelamento → `canceled`

## Próxima fatia

- Configuração fiscal do emitente, UF, CSC/idToken e ambiente.
- Cofre do certificado A1, sem senha no SQLite ou em variável de log.
- Gerador/assinador XML NFC-e 4.00 com validação XSD.
- Gerador fiscal e consulta por chave antes de qualquer reenvio ambíguo.
- DANFE NFC-e 80 mm com QR Code e indicação visível de contingência.
- Sincronização de `fiscal_documents` e `fiscal_events` com o PostgreSQL.

SAT CF-e (modelo 59) permanece separado: o modelo aparece no esquema para não
forçar migração destrutiva, mas o hardware SAT exige outro adaptador e não será
tratado como se fosse NFC-e.
