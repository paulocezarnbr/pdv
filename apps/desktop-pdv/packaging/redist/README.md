# Pré-requisitos embarcados no instalador

Coloque aqui os redistribuíveis. **Não são versionados no git** (binários de
terceiros, dezenas de MB) — o pipeline de CI os baixa antes de rodar o Inno Setup.

| Arquivo | Origem | Por que é necessário |
|---|---|---|
| `VC_redist.x64.exe` | https://aka.ms/vs/17/release/vc_redist.x64.exe | O Qt (PySide6) depende do Visual C++ Runtime. Sem ele o `PDV.exe` encerra **sem mensagem de erro** em máquina recém-formatada |

## Baixar manualmente

```powershell
curl.exe -L -o packaging\redist\VC_redist.x64.exe https://aka.ms/vs/17/release/vc_redist.x64.exe
```

## Por que embarcar em vez de baixar na hora

Implantação em loja costuma acontecer antes de a internet estar configurada —
ou com a internet que vai justamente ser testada pelo PDV. Um instalador que
depende de download escolhe o pior momento possível para falhar.
