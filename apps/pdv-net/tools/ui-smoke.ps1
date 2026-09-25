<#
.SYNOPSIS
    Dirige o PDV.exe (WinUI) pela UI Automation, como um operador faria.

.DESCRIPTION
    O banco é criado pelo migrate() do PDV em Python, com uma operadora cujo PIN
    o Python cadastrou (Argon2id). O roteiro:

      1. o caixa abre na tela de login com o nome da loja;
      2. PIN errado mostra "Login ou PIN inválido." e limpa o campo;
      3. o PIN certo, digitado no teclado da tela, entra no caixa.

    Os testes do Pdv.App já provam a lógica sem janela. Isto prova o que só o
    binário prova: que a tela liga os botões aos comandos certos, que o
    Argon2 roda fora da thread da tela e que o executável self-contained abre.

.EXAMPLE
    .\tools\ui-smoke.ps1 -Exe .\src\Pdv.WinUI\bin\x64\Debug\net10.0-windows10.0.19041.0\win-x64\PDV.exe
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)] [string] $Exe)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

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
    $reason = "Falha ao criar o banco pelo PDV em Python: " + (($seedOutput | Select-Object -Last 3) -join ' | ')
    if ($env:GITHUB_ACTIONS) { Write-Host "::error title=ui-smoke::$reason" }
    throw $reason
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
    $lines = @()
    $process.Refresh()
    $lines += if ($process.HasExited) { "o processo saiu com código $($process.ExitCode)" } else { 'o processo continua rodando' }
    if (Test-Path $crashLog) { $lines += 'log do PDV: ' + ((Get-Content $crashLog -Raw -Encoding UTF8) -replace '\s+', ' ') }
    $windows = $A::RootElement.FindAll($Tree::Children, [System.Windows.Automation.Condition]::TrueCondition) |
        ForEach-Object { "'$($_.Current.Name)' (pid $($_.Current.ProcessId))" } | Select-Object -First 12
    $lines += 'janelas na sessão: ' + ($windows -join ', ')
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
        # O log da execução no GitHub exige login; a anotação aparece no resumo público.
        if ($env:GITHUB_ACTIONS) { Write-Host "::error title=ui-smoke::$failure" }
    }
    exit 1
}
Write-Host 'Tela de login: ok'
