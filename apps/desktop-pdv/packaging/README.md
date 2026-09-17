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
dist\installer\PDV-Setup-1.0.0.exe     instalador
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
