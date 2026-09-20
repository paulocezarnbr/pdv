# Contexto Fiscal — NFC-e offline-first

## Limite desta fase

O núcleo fiscal não confunde **reservar** com **autorizar**. O PDV já consegue
reservar série/número e registrar uma emissão normal ou em contingência, mas só
um adaptador estadual, falando com a SEFAZ e usando certificado válido, poderá
mover o documento para `authorized`.

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
- Adaptadores por UF, timeout explícito, consulta por chave antes de reenviar.
- DANFE NFC-e 80 mm com QR Code e indicação visível de contingência.
- Sincronização de `fiscal_documents` e `fiscal_events` com o PostgreSQL.

SAT CF-e (modelo 59) permanece separado: o modelo aparece no esquema para não
forçar migração destrutiva, mas o hardware SAT exige outro adaptador e não será
tratado como se fosse NFC-e.
