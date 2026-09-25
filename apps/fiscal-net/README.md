# Serviço fiscal (NFC-e 4.00)

Emissor interno de NFC-e em C# (.NET 10) com a biblioteca DFe.NET (Zeus). Só
a retaguarda fala com ele, pela rede do Coolify, com `FISCAL_SERVICE_TOKEN`.

- `GET /health`, `POST /v1/fiscal/authorize` e `POST /v1/fiscal/status`. É o
  mesmo contrato do serviço em Python que ele substituiu.
- **QR Code v3** por padrão, sem CSC. O v2 fica com `FISCAL_QRCODE_VERSION=2`.
- **Idempotência.** O estado fica em SQLite (`FISCAL_STATE_DB`). O XML
  assinado é gravado antes de transmitir, e a consulta por chave resolve o que
  ficou sem resposta.
- **Cofre.** O A1 (`.pfx`) e a senha (`.pfx.senha`) ficam em
  `FISCAL_SECRETS_DIR`. A retaguarda só manda o nome.

```bash
dotnet test
```

```bash
docker build -t erp-fiscal .
```

Decisões, estados e o que o emissor calcula: `docs/fiscal_architecture.md`.
Deploy: `apps/cloud-api/COOLIFY.md`.
