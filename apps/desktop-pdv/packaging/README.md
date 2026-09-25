# Empacotamento e Endurecimento do PDV

Este diretório produz o `.exe` instalável e aplica as permissões NTFS. Antes do
"como", o "quanto": cada camada aqui tem um limite real, e vender proteção que
não existe é pior do que não ter proteção nenhuma.

---

## O que cada camada realmente entrega

| Objetivo | Status | O que protege | O que **não** protege |
|---|---|---|---|
| App vira `.exe` | ✅ Resolvido | Distribuição sem Python instalado | — |
| Só admin altera os arquivos do programa | ✅ Resolvido | Operador não troca `.exe`, `.dll` nem `.pyc` | Um administrador da máquina |
| Impedir leitura do código-fonte | ⚠️ **Mitigado, não resolvido** | Docstrings removidas; não há `.py` no pacote | Bytecode é extraível e desmontável |
| Proteger o banco de vendas | ⚠️ Parcial | Detecção de adulteração (HMAC + nuvem) | O operador **consegue** abrir o `.db` |

### Por que "impedir a leitura do código" não é alcançável

O código precisa ser executado na máquina do cliente. Logo, tudo que é preciso
para executá-lo está nessa máquina. Isso vale para Python, Java, C# e até para C
compilado — muda apenas o custo do ataque, nunca a possibilidade.

Medido neste projeto, com o build padrão:

```
Docstrings no binário:      removidas  (optimize=2 / -OO)
Arquivos .py no pacote:     nenhum
Bytecode extraível:         SIM — 373 linhas desmontáveis para um único módulo
```

O que o build padrão consegue: eliminar os comentários e a documentação interna
(que neste projeto explicam as regras de negócio e o modelo de ameaça), e exigir
do curioso ferramenta específica em vez de um Bloco de Notas.

O que ele **não** consegue: impedir que alguém decidido recupere a lógica.

**Se proteger a lógica for requisito real, use o backend Nuitka:**

```powershell
.\packaging\build.ps1 -Backend nuitka
```

Nuitka traduz o Python para C e compila para código de máquina. Não existe
`.pyc` para extrair nem bytecode para desmontar — a recuperação passa a exigir
engenharia reversa de binário nativo. Custa 10–25 minutos de compilação.
Continua não sendo inviolável; passa a ser caro o bastante para não valer a pena.

---

## Modelo de permissões instalado

```
C:\Program Files\ERPFood\PDV\          SYSTEM + Administradores : total
   PDV.exe, _internal\, tools\         Usuários                 : ler + executar
                                       → operador NÃO troca binário

C:\ProgramData\ERPFood\PDV\            Usuários : modificar
   pdv_local.db                        → PRECISA ser gravável (o app vende como usuário)

C:\ProgramData\ERPFood\PDV\logs\       Usuários : append-only (AD sem WD)
                                       → pode crescer, não pode ser apagado
```

### A tensão que não dá para esconder

Você pediu que só o administrador mexesse nos arquivos internos. Para o
**programa**, isso está resolvido e verificado. Para o **banco de dados**, há um
conflito inerente:

> O app roda como o usuário do caixa. Para gravar a venda, esse usuário precisa
> de permissão de escrita no `.db`. Quem tem permissão de escrita pelo app
> também tem pelo DB Browser for SQLite.

Não existe ACL que distinga "escrita vinda do meu programa" de "escrita vinda de
outro programa" — o NTFS concede permissão a *usuários*, não a *aplicações*.

**Três saídas reais, em ordem de custo:**

1. **Detecção (implementada).** A cadeia HMAC do ledger e a ancoragem no
   servidor tornam qualquer alteração evidente. A auditoria SACL do Windows
   registra no Log de Segurança quem abriu o arquivo para escrita — inclusive
   um administrador. Ver `services/audit.py`.

2. **Serviço Windows (recomendado para redes com operador não confiável).**
   Um serviço rodando como `LocalSystem` torna-se o único dono do banco; a ACL
   do `.db` **exclui** o grupo Usuários por completo. A interface gráfica passa
   a falar com o serviço por named pipe local. O operador deixa de ter qualquer
   caminho até o arquivo. Custo: um processo a mais e uma camada de IPC.

3. **SQLCipher.** Criptografa o arquivo em repouso, com a chave no DPAPI. Impede
   a edição casual com editor de SQLite. Não impede quem anexa um depurador ao
   processo e extrai a chave da memória.

O sistema hoje entrega a opção 1. A 2 é a evolução natural se o cenário exigir.

### Sobre o administrador da máquina

Um administrador local do Windows pode: reescrever qualquer ACL, tomar posse de
qualquer arquivo, anexar depurador a qualquer processo e ler a memória. Nenhuma
proteção *local* resiste a isso — é a definição do papel.

Por isso a garantia forte do sistema **não está na máquina**: está na ancoragem
no servidor. Uma vez sincronizada, a venda tem cópia fora do alcance do
operador e do administrador da loja. A janela de exposição é o intervalo entre
a venda e o ACK da nuvem — motivo pelo qual o worker de sincronização sobe em
segundos, e não em minutos. É uma decisão de segurança, não de desempenho.

---

## Como compilar

### Pelo GitHub, sem máquina Windows (recomendado)

O workflow **Instalador do PDV** (`.github/workflows/installer.yml`) roda este
mesmo `build.ps1` num Windows limpo: instala só os pins, baixa o Visual C++
Redistributable e **confere a assinatura da Microsoft** nele, roda a suíte,
compila, executa o `--selftest` dentro do `PDV.exe` e gera o instalador.

* Roda sozinho a cada push que mexe em `apps/desktop-pdv/`, e sob demanda em
  **Actions → Instalador do PDV → Run workflow**.
* O `PDV-Setup-x.y.z.exe` e o `.sha256` ficam em **Artifacts** da execução
  (30 dias).
* Numa tag `pdv-v1.2.3`, vira também uma **Release**.
* Com os secrets `PDV_SIGN_CERT_BASE64` (o `.pfx` em base64) e
  `PDV_SIGN_PASSWORD`, sai assinado. Sem eles, sai sem assinatura e o
  SmartScreen mostra "Editor desconhecido" — clique em *Mais informações →
  Executar assim mesmo*.

**Suba a versão** em `installer.iss` a cada entrega que mude o schema local:
o bloqueio de downgrade compara essa versão. `tests/test_packaging.py` reprova
se `installer.iss`, `version_info.txt` e `build.ps1` divergirem.

### Pré-requisitos

```powershell
pip install -r requirements.txt
pip install pyinstaller          # backend padrão
pip install nuitka               # opcional: proteção forte de código
```

Inno Setup 6.2+: https://jrsoftware.org/isdl.php

### Build completo

```powershell
.\packaging\build.ps1
```

Com proteção de código e assinatura digital:

```powershell
.\packaging\build.ps1 -Backend nuitka -SignCert .\certificado.pfx
```

O pipeline roda os testes antes de empacotar e **aborta se algum falhar**.

### Saída

```
dist\PDV\PDV.exe                       o caixa (onedir)
dist\PDV\PDVSetup.exe                  assistente de instalação
dist\installer\PDV-Setup-1.1.5.exe     instalador
```

Os dois executáveis dividem o mesmo diretório e, portanto, as mesmas DLLs do Qt
e do Python: ~122 MB no total contra ~240 MB se fossem dois `onedir` separados.

### O que o `PDVSetup.exe` faz

O instalador copia arquivos; ele é quem entrega um caixa que comprovadamente
vende. Roda **depois** do `harden.ps1` — é o endurecimento que concede escrita
em `ProgramData`, e na ordem inversa o banco nasceria com a ACL herdada.

1. cria o banco e aplica as migrations;
2. gera o `device_secret` e o protege com DPAPI em escopo de máquina;
3. varre as portas seriais e identifica a balança pelo protocolo que responde;
4. localiza a impressora térmica **por modelo**, nunca por marca;
5. roda o teste de fumaça e imprime um cupom com acentuação, corte e gaveta.

| Saída | Significado | O instalador |
|---|---|---|
| 0 | tudo verificado | segue |
| 2 | pendências (balança desligada, sem térmica) | segue e relata |
| 3 | não é possível vender | segue e alerta |

O relatório completo fica sempre em
`C:\ProgramData\ERPFood\PDV\logs\setup.log`, em UTF-8.

Uso avulso, para suporte:

```powershell
& "C:\Program Files\ERPFood\PDV\PDVSetup.exe" --detect-only
```

`--detect-only` redetecta os periféricos sem tocar em catálogo nem em
sincronização — é o que o atalho "Reconfigurar periféricos" do menu Iniciar
chama quando a balança é trocada ou o cabo USB muda de porta.

### Ativação do terminal

Implantação em massa:

```powershell
.\PDV-Setup-1.1.5.exe /SILENT /ACTIVATIONCODE=A1B2C3D4
```

Na instalação interativa o próprio `PDVSetup.exe` pergunta numa caixa de
diálogo — o código é gerado no painel na hora e ditado para quem está na loja,
então o instalador não teria como conhecê-lo de antemão.

Ativar **não** é obrigatório. Sem código o PDV instala, vende e acumula na fila
de saída; só não sincroniza. O token recebido vai para o cofre DPAPI, nunca para
`device_settings`.

> **Terminal com fila pendente não troca de tenant.** Aquelas vendas foram
> registradas sob o CNPJ antigo; reapontar antes de esvaziar a fila mandaria o
> faturamento de uma loja para outra — erro que só aparece na conciliação fiscal
> do mês. Sincronize primeiro.

### Atualização

Basta rodar o instalador novo por cima. O `{app}` é substituído; `ProgramData`
(banco, fila de sincronização, ativação, logs) não é tocado. O instalador fecha
o PDV antes de gravar — daí o aviso para **fechar o caixa** primeiro — e recusa
downgrade, porque um binário antigo pode não entender migrations já aplicadas
no banco da loja.

### Instalação existente: atualizar ou reparar

Com o PDV já instalado, o instalador mostra a página **Instalação existente
encontrada** com o que encontrou:

- a versão no registro do Windows e a versão gravada no próprio `PDV.exe`. Se
  as duas divergem, a instalação anterior ficou pela metade, e os arquivos são
  reinstalados;
- a pasta do programa, e o banco da loja com o tamanho dele, que é sempre
  preservado.

| Instalado | Opções |
|---|---|
| versão anterior | **Atualizar** para a nova e refazer as permissões |
| mesma versão | **Reparar tudo** (reinstala os arquivos) ou **reparar só as permissões e conferir o banco** (rápido, não troca arquivos) |
| versão mais nova | bloqueado: downgrade |

Em todos os casos o `harden.ps1` roda de novo. É ele que devolve ao caixa a
escrita na pasta de dados. Se o PDV abrir com "attempt to write a readonly
database", rode o mesmo instalador e escolha o reparo. Por linha de comando,
para suporte remoto:

```powershell
.\PDV-Setup-1.1.5.exe /SILENT /REPARO=permissoes
```

Até a 1.1.4 a detecção nunca funcionou: o `[Code]` procurava a chave do
desinstalador com o `{{` do `AppId`, que só é escape no `[Setup]`. Toda
instalação por cima era tratada como nova.

A redetecção de periféricos numa atualização só preenche o que estiver vazio.
Uma balança desligada na hora do update não rebaixa para `simulated` um terminal
que estava vendendo.

> **`--demo` nunca em loja real.** A flag carrega o catálogo de demonstração; o
> provisionamento normal deixa o catálogo vazio, porque os produtos descem na
> primeira sincronização com a retaguarda. Semear a confeitaria de exemplo no
> PDV do cliente criaria itens fantasma aparecendo na busca do operador durante
> uma venda de verdade.

---

## Decisões de empacotamento

**`onedir`, não `onefile`.** Onefile extrai tudo para `%TEMP%` a cada execução —
diretório onde o operador tem escrita total. Isso anularia toda a ACL do
instalador: bastaria trocar um `.pyd` no intervalo entre extração e carga.
Onefile ainda adiciona 3–8 s a cada abertura do caixa.

**Sem UPX.** Compressor de executável dispara heurística de antivírus. Num PDV,
isso significa o app sumir da máquina do cliente numa atualização de definições,
às sete da manhã de um sábado.

**Sem `uac_admin` no app.** Exigir elevação para *vender* treinaria o operador a
aprovar UAC no automático — o que é uma brecha, não uma proteção. Elevação só na
instalação e na atualização.

**Banco preservado na desinstalação.** Reinstalar é rotina de suporte; apagar
vendas ainda não sincronizadas junto com o programa destruiria o faturamento do
dia. A remoção dos dados é sempre ato manual e deliberado.

---

## Assinatura de código

Sem certificado de Assinatura de Código, o SmartScreen exibe "Editor
desconhecido" e parte dos clientes não conclui a instalação. Além da confiança,
a assinatura permite detectar adulteração do executável já instalado.

Use sempre `/tr` (carimbo de tempo): sem ele, a assinatura expira junto com o
certificado e o binário já instalado passa a acusar erro.

---

## Compilar o executavel

Um comando, do diretorio `apps/desktop-pdv`:

```powershell
.\packaging\build.ps1
```

O pipeline tem cinco etapas e **nenhuma delas e opcional**:

| # | Etapa | Por que ela existe |
|---|-------|--------------------|
| 0 | Testes | Nao se empacota codigo quebrado. |
| 1 | Limpeza | Artefato antigo no `dist/` ja mascarou um arquivo que deixou de ser gerado. |
| 2 | Compilacao | `onedir`, nunca `onefile` — ver o cabecalho de `pdv.spec`. |
| 3 | **Autoteste dentro do pacote** | Ver abaixo. |
| 4 | Assinatura | Sem ela o SmartScreen diz "Editor desconhecido". |
| 5 | Instalador | Inno Setup. |

Saida: `dist\PDV\PDV.exe` (o caixa) e `dist\PDV\PDVSetup.exe` (o assistente),
mais `dist\installer\*.exe` se o Inno Setup estiver instalado.

### Por que a etapa 3 existe

A suite roda contra o **codigo-fonte**, onde todo arquivo esta no lugar e todo
modulo e importavel. O executavel empacotado e outro programa: o PyInstaller
monta a arvore de imports por analise estatica, e **todo import tardio e
invisivel para ela**.

Este projeto esta cheio de imports tardios, cada um por um bom motivo:

* `argon2` e `cryptography` sao importados dentro de funcoes para que a
  ausencia delas degrade o sistema em vez de impedir o caixa de abrir;
* o `uvicorn` resolve loop, protocolo e ciclo de vida **por string**, em tempo
  de execucao;
* `schema.sql` e o app do garcom sao lidos por caminho relativo ao modulo.

Um `hiddenimports` incompleto produz um pacote que **instala e abre**, e falha
depois — o gerente descobre que nao consegue autorizar um cancelamento na frente
do cliente, ou o garcom abre o app e recebe um 500. A suite verde nao diz nada
sobre isso, porque ela nunca rodou dentro do pacote.

`PDV.exe --selftest` roda no binario entregue e responde uma pergunta so:
*este pacote esta completo?* O relatorio vai para `dist\PDV\selftest.log`
(o `PDV.exe` e compilado sem console, entao sem o arquivo ele se perderia).

Cada verificacao diz o que quebra na loja se ela falhar:

```
  ok      schema.sql         23719 bytes
  ok      migrations         banco novo na versao 6
  ok      app do garcom      40 KiB + vendor
  ok      Argon2id           hash e verificacao
  ok      TLS do salao       4B 72 92 6B
  ok      uvicorn            4 modulos resolvidos por string
  ok      rotas do salao     22 rotas
  ok      impressora         backend de arquivo
  ok      porta serial       1 porta(s) COM
  ok      interface          janelas importaveis
```

### Compilar so o binario, sem instalador

```powershell
.\packaging\build.ps1 -SkipInstaller
```

### Proteger a logica de negocio

O PyInstaller empacota bytecode `.pyc`, que **e extraivel**. Quando isso
importar:

```powershell
.\packaging\build.ps1 -Backend nuitka
```

Nuitka traduz para C e compila nativamente — nao ha `.pyc` para extrair.
Compilacao de 10 a 25 minutos.
