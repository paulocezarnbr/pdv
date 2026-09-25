<#
.SYNOPSIS
    Dirige o PDV.exe (WinUI) pela UI Automation, como um operador faria.

.DESCRIPTION
    O banco é criado pelo migrate() do PDV em Python, com uma operadora cujo PIN
    o Python cadastrou (Argon2id). O roteiro:

      1. o caixa abre na tela de login com o nome da loja;
      2. PIN errado mostra "Login ou PIN inválido." e limpa o campo;
      3. o PIN certo, digitado no teclado da tela, entra no caixa;
      4. o código de barras entra na venda, e o débito (TEF simulado) fecha
         a venda com a conversa do TEF na tela.

    Os testes do Pdv.App já provam a lógica sem janela. Isto prova o que só o
    binário prova: que a tela liga os botões aos comandos certos, que o
    Argon2 roda fora da thread da tela e que o executável self-contained abre.

.EXAMPLE
    .\tools\ui-smoke.ps1
    .\tools\ui-smoke.ps1 -Exe .\src\Pdv.WinUI\bin\x64\Debug\net10.0-windows10.0.19041.0\win-x64\PDV.exe
#>
[CmdletBinding()]
param([string] $Exe)

$ErrorActionPreference = 'Stop'

function Annotate([string] $message) {
    # O log da execução no GitHub exige login; a anotação aparece no resumo
    # público. Quebra de linha e % precisam de escape, senão a mensagem corta.
    if ($env:GITHUB_ACTIONS) {
        $escaped = $message -replace '%', '%25' -replace "`r", '%0D' -replace "`n", '%0A'
        Write-Host "::error title=ui-smoke::$escaped"
    }
}

# Qualquer erro fatal, em qualquer linha, sai com o motivo e o lugar — e não
# com um "exit code 1" que obriga a adivinhar.
trap {
    Annotate ("linha $($_.InvocationInfo.ScriptLineNumber): $($_.Exception.Message)")
    Write-Host "FALHOU  linha $($_.InvocationInfo.ScriptLineNumber): $($_.Exception.Message)"
    exit 1
}

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

# Sem -Exe, o PDV.exe mais novo da saída do build. Procurar aqui, e não no
# workflow com Resolve-Path, é o que faz um caminho errado virar uma anotação
# com a lista do que existe — e não um "exit code 1" antes da primeira linha.
if (-not $Exe) {
    $bin = Join-Path $PSScriptRoot '..\src\Pdv.WinUI\bin'
    $found = Get-ChildItem $bin -Recurse -Filter PDV.exe -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $found) {
        $tree = Get-ChildItem $bin -Recurse -Directory -ErrorAction SilentlyContinue |
            Select-Object -First 8 | ForEach-Object { $_.FullName }
        throw "PDV.exe não foi gerado em $bin. Pastas: $($tree -join ', ')"
    }
    $Exe = $found.FullName
}
if (-not (Test-Path $Exe)) { throw "PDV.exe não existe em '$Exe'." }
Write-Host "executável: $Exe"

$root = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
$work = Join-Path ([IO.Path]::GetTempPath()) ("pdv-ui-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory $work | Out-Null
$db = Join-Path $work 'pdv_local.db'

# O banco e a operadora pelo próprio PDV em Python.
$env:PYTHONPATH = Join-Path $root 'apps\desktop-pdv\src'
$seed = @"
from pathlib import Path
from pdv.data.database import Database
from pdv.services.authorization import hash_pin
db = Database(Path(r'$db')); db.migrate()
with db.transaction() as c:
    c.execute("INSERT INTO users (id, tenant_id, name, login, role, pin_hash, can_authorize, is_active, updated_at) "
              "VALUES ('u-1', '11111111-1111-1111-1111-111111111111', 'Ana Caixa', 'ana', 'cashier', ?, 0, 1, '2026-09-25T12:00:00.000+00:00')",
              (hash_pin('480362'),))
    c.execute("INSERT INTO device_settings (key, value, updated_at) VALUES ('store.name', 'Dolce Affetto', 'x')")
    c.execute("INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, pricing_mode, price_cents, is_active, updated_at) "
              "VALUES ('p-1', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', "
              "'F1', '7890000000011', 'Fatia de torta', 'unit', 1450, 1, 'x')")
db.close()
"@
# Por arquivo, e não por -c: o PowerShell 5.1 come as aspas duplas ao passar
# argumento para programa nativo.
$seedFile = Join-Path $work 'seed.py'
Set-Content -Path $seedFile -Value $seed -Encoding UTF8
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # stderr do python não pode virar exceção antes de ser lido
$seedOutput = & python $seedFile 2>&1 | ForEach-Object { "$_" }
$seedCode = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($seedCode -ne 0) {
    throw ("Falha ao criar o banco pelo PDV em Python: " + (($seedOutput | Select-Object -Last 3) -join ' | '))
}

$A = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
function Find([System.Windows.Automation.AutomationElement] $scope, [string] $id, [int] $seconds = 15) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, $id)
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        $element = $scope.FindFirst($Tree::Descendants, $condition)
        if ($element) { return $element }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    throw "Elemento '$id' não apareceu em $seconds s."
}
function Click([System.Windows.Automation.AutomationElement] $element) {
    $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function TypePin($window, [string] $pin) { foreach ($digit in $pin.ToCharArray()) { Click (Find $window "Key$digit") } }
function Text($element) { $element.Current.Name }

$env:PDV_DB_PATH = $db
# O PDV.exe não tem console: o erro de abertura só aparece neste arquivo.
$crashLog = Join-Path $work 'pdv-winui.log'
$env:PDV_CRASH_LOG = $crashLog
$process = Start-Process -FilePath $Exe -PassThru
$failures = @()

function Diagnose {
    # Cada pedaço por conta própria: o diagnóstico não pode ser a próxima falha.
    $lines = @()
    try {
        $process.Refresh()
        $lines += if ($process.HasExited) { "o processo saiu com código $($process.ExitCode)" } else { 'o processo continua rodando' }
    } catch { $lines += "estado do processo: $($_.Exception.Message)" }
    try {
        if (Test-Path $crashLog) { $lines += 'log do PDV: ' + ((Get-Content $crashLog -Raw -Encoding UTF8) -replace '\s+', ' ') }
        else { $lines += 'o PDV não gravou log de erro' }
    } catch { $lines += "log do PDV: $($_.Exception.Message)" }
    try {
        $windows = $A::RootElement.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { "'$($_.Current.Name)' (pid $($_.Current.ProcessId))" } | Select-Object -First 12
        $lines += 'janelas na sessão: ' + ($windows -join ', ')
    } catch { $lines += "janelas: $($_.Exception.Message)" }
    return $lines
}

try {
    $windowCondition = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $process.Id)
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $window = $A::RootElement.FindFirst($Tree::Children, $windowCondition)
        Start-Sleep -Milliseconds 300
    } while (-not $window -and (Get-Date) -lt $deadline)
    if (-not $window) { throw 'A janela do PDV não abriu em 30 s.' }

    # 1. abre no login, com a loja
    $store = Text (Find $window 'StoreName')
    if ($store -ne 'Dolce Affetto') { $failures += "loja: esperado 'Dolce Affetto', veio '$store'" }
    else { Write-Host 'ok  abre no login com o nome da loja' }

    $login = Find $window 'Login'
    $login.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('ana')

    # 2. PIN errado
    TypePin $window '999999'
    Click (Find $window 'SignIn')
    $errorBar = Find $window 'Error'
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $message = ($errorBar.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { $_.Current.Name }) -join ' '
        Start-Sleep -Milliseconds 200
    } while ($message -notmatch 'inválido' -and (Get-Date) -lt $deadline)
    if ($message -notmatch 'Login ou PIN inválido') { $failures += "PIN errado: mensagem '$message'" }
    else { Write-Host 'ok  PIN errado mostra o motivo' }

    # 3. PIN certo, pelo teclado da tela
    TypePin $window '480362'
    Click (Find $window 'SignIn')
    $greeting = Text (Find $window 'Greeting' 20)
    if ($greeting -ne 'Olá, Ana') { $failures += "entrada: esperado 'Olá, Ana', veio '$greeting'" }
    else { Write-Host 'ok  PIN certo entra no caixa' }

    # 4. bipa o código de barras e vende no débito (TEF simulado)
    $query = Find $window 'Query'
    $query.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('7890000000011')
    Click (Find $window 'Scan')
    $deadline = (Get-Date).AddSeconds(10)
    do { $total = Text (Find $window 'Total'); Start-Sleep -Milliseconds 200 } while ($total -notmatch '14,50' -and (Get-Date) -lt $deadline)
    if ($total -notmatch '14,50') { $failures += "total: esperado R$ 14,50, veio '$total'" }
    else { Write-Host 'ok  código de barras entra na venda' }

    Click (Find $window 'PayDebit')
    $notice = Find $window 'Notice'
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $message = ($notice.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
            ForEach-Object { $_.Current.Name }) -join ' '
        Start-Sleep -Milliseconds 200
    } while ($message -notmatch 'Venda finalizada' -and (Get-Date) -lt $deadline)
    $tef = ((Find $window 'TefMessages').FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
        ForEach-Object { $_.Current.Name }) -join ' | '
    if ($message -notmatch 'Venda finalizada') { $failures += "débito: aviso '$message'; TEF: '$tef'" }
    elseif ($tef -notmatch 'Transação aprovada') { $failures += "débito: a conversa do TEF não apareceu ('$tef')" }
    else { Write-Host 'ok  venda no débito, com a conversa do TEF na tela' }
}
catch {
    $failures += $_.Exception.Message
    $failures += Diagnose
}
finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    Start-Sleep -Milliseconds 500
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
    foreach ($failure in $failures) {
        Write-Host "FALHOU  $failure"
        Annotate $failure
    }
    exit 1
}
Write-Host 'Caixa: ok (login, venda e TEF)'
