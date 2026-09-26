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
    # Gerente com PIN e teto de 30%, e uma torta por quilo com ficha, para a
    # balança simulada (847 g), o desconto e o cancelamento.
    c.execute("INSERT INTO users (id, tenant_id, name, login, role, pin_hash, can_authorize, max_discount_percent, is_active, updated_at) "
              "VALUES ('u-2', '11111111-1111-1111-1111-111111111111', 'Bruno Gerente', 'bruno', 'manager', ?, 1, '30', 1, 'x')",
              (hash_pin('730514'),))
    c.execute("PRAGMA defer_foreign_keys = ON")
    c.execute("INSERT INTO inventory_items (id, tenant_id, store_id, name, unit, balance_mg, min_stock_mg, avg_cost_cents_per_kg, updated_at) "
              "VALUES ('i-1', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'Farinha', 'mg', 5000000, 0, 520, 'x')")
    c.execute("INSERT INTO recipes (id, tenant_id, product_id, base_qty_g, yield_factor, updated_at) "
              "VALUES ('r-1', '11111111-1111-1111-1111-111111111111', 'p-2', 1000, '1', 'x')")
    c.execute("INSERT INTO recipe_lines (id, recipe_id, inventory_item_id, qty_per_base_mg, waste_percent, updated_at) "
              "VALUES ('l-1', 'r-1', 'i-1', 250000, '0', 'x')")
    c.execute("INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, pricing_mode, price_cents, tare_grams, recipe_id, is_active, updated_at) "
              "VALUES ('p-2', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', "
              "'T1', '2000000000017', 'Torta por quilo', 'weight', 4990, 0, 'r-1', 1, 'x')")
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

function WaitWindow($proc) {
    $windowCondition = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $proc.Id)
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $found = $A::RootElement.FindFirst($Tree::Children, $windowCondition)
        Start-Sleep -Milliseconds 300
    } while (-not $found -and (Get-Date) -lt $deadline)
    if (-not $found) { throw 'A janela do PDV não abriu em 30 s.' }
    return $found
}

# Diálogos da tela (ContentDialog): texto, autorização por PIN e avisos.
function Named($name, [int] $seconds = 10) {
    $condition = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, $name)
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        # O ContentDialog abre numa camada própria da janela.
        $found = $window.FindFirst($Tree::Descendants, $condition)
        if (-not $found) { Start-Sleep -Milliseconds 200 }
    } while (-not $found -and (Get-Date) -lt $deadline)
    if (-not $found) { throw "Botão '$name' não apareceu em $seconds s." }
    return $found
}
function SetText($element, [string] $text, [string] $what) {
    $deadline = (Get-Date).AddSeconds(5)
    while ($true) {
        try { $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($text); return }
        catch {
            if ((Get-Date) -gt $deadline) { throw "$what recusou o texto: $($_.Exception.Message)" }
            Start-Sleep -Milliseconds 250
        }
    }
}
function Answer([string] $text) {
    SetText (Find $window 'DialogText') $text 'a caixa de texto do diálogo'
    Click (Named 'Confirmar')
    Start-Sleep -Milliseconds 400
}
function Authorize([string] $login, [string] $pin) {
    # A caixa de seleção editável guarda o texto num campo interno.
    $combo = Find $window 'AuthorizerLogin'
    $edit = $combo.FindFirst($Tree::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)))
    SetText $(if ($edit) { $edit } else { $combo }) $login 'o login do diálogo de autorização'
    SetText (Find $window 'AuthorizerPin') $pin 'o PIN do diálogo de autorização'
    Click (Named 'Autorizar')
    Start-Sleep -Milliseconds 600
}
function NoticeText {
    ((Find $window 'Notice').FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
        ForEach-Object { $_.Current.Name }) -join ' '
}

$stub = $null
try {
    $window = WaitWindow $process

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

    # 3. PIN certo, pelo teclado da tela, e o fundo de troco da abertura do caixa
    TypePin $window '480362'
    Click (Find $window 'SignIn')
    Answer '100,00'
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

    # 4a. o cupom da venda foi para a fila e, sem impressora configurada, para a pasta
    $deadline = (Get-Date).AddSeconds(10)
    do { $receipts = @(Get-ChildItem (Join-Path $work 'cupons') -Filter '*.txt' -ErrorAction SilentlyContinue); Start-Sleep -Milliseconds 200 } while ($receipts.Count -lt 1 -and (Get-Date) -lt $deadline)
    $receiptText = if ($receipts.Count -gt 0) { Get-Content $receipts[0].FullName -Raw -Encoding UTF8 } else { '' }
    if ($receiptText -notmatch 'CUPOM NAO FISCAL' -or $receiptText -notmatch 'Cartao Debito' -or $receiptText -notmatch 'Fatia de torta') { $failures += "cupom: '$receiptText'" }
    else { Write-Host 'ok  cupom da venda gravado pela fila de impressão' }

    # 4b. cliente pelo telefone (cadastro na hora), carga pré-paga com o PIN do
    #     gerente e uma venda paga com o pré-pago
    Click (Find $window 'IdentifyCustomer')
    Answer '(21) 99999-0000'
    Answer 'Lia Cliente'
    $summary = Text (Find $window 'CustomerSummary')
    Click (Find $window 'DepositPrepaid')
    Answer '20,00'
    Authorize 'bruno' '730514'
    $query.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('7890000000011')
    Click (Find $window 'Scan')
    Start-Sleep -Milliseconds 500
    Click (Find $window 'PayPrepaid')
    $deadline = (Get-Date).AddSeconds(10)
    do { $message = NoticeText; Start-Sleep -Milliseconds 200 } while ($message -notmatch 'Saldo pré-pago' -and (Get-Date) -lt $deadline)
    if ($summary -notmatch '^Lia Cliente') { $failures += "cliente: '$summary'" }
    elseif ($message -notmatch 'Saldo pré-pago: R\$ 5,50') { $failures += "pré-pago: '$message'" }
    else { Write-Host 'ok  cliente pelo telefone, carga com PIN e venda no pré-pago' }

    # 5. item pesado pela balança simulada (847 g), desconto de 10% com o PIN
    #    do gerente e cancelamento do item, pelos diálogos da tela.
    $deadline = (Get-Date).AddSeconds(15)
    do { $scale = Text (Find $window 'Scale'); Start-Sleep -Milliseconds 200 } while ($scale -notmatch '0,847' -and (Get-Date) -lt $deadline)
    if ($scale -notmatch '0,847 kg') { $failures += "balança: o mostrador diz '$scale'" }
    else { Write-Host 'ok  balança simulada no mostrador' }

    $deadline = (Get-Date).AddSeconds(15)
    do {
        $query.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('2000000000017')
        Click (Find $window 'Scan')
        Start-Sleep -Milliseconds 500
        $total = Text (Find $window 'Total')
    } while ($total -notmatch '42,27' -and (Get-Date) -lt $deadline)
    if ($total -notmatch '42,27') { $failures += "peso: esperado R$ 42,27 por 847 g, veio '$total'" }
    else { Write-Host 'ok  item pesado cobra o peso estável' }

    Click (Find $window 'Discount6')
    Answer '10'
    Authorize 'bruno' '730514'
    Answer 'cliente fiel'
    $deadline = (Get-Date).AddSeconds(10)
    do { $total = Text (Find $window 'Total'); Start-Sleep -Milliseconds 200 } while ($total -notmatch '38,04' -and (Get-Date) -lt $deadline)
    $message = NoticeText
    if ($total -notmatch '38,04' -or $message -notmatch 'autorizado por Bruno Gerente') { $failures += "desconto: total '$total', aviso '$message'" }
    else { Write-Host 'ok  desconto de 10% com o PIN do gerente' }

    $line = (Find $window 'Lines').FindFirst($Tree::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::ListItem)))
    $line.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    Start-Sleep -Milliseconds 300
    Click (Find $window 'CancelItem')
    Answer 'cliente desistiu'
    Authorize 'bruno' '730514'
    $deadline = (Get-Date).AddSeconds(10)
    do { $total = Text (Find $window 'Total'); Start-Sleep -Milliseconds 200 } while ($total -notmatch '0,00' -and (Get-Date) -lt $deadline)
    $message = NoticeText
    if ($total -notmatch '0,00' -or $message -notmatch 'Item cancelado') { $failures += "cancelamento: total '$total', aviso '$message'" }
    else { Write-Host 'ok  cancelamento do item com o PIN do gerente' }

    # 6. fechamento cego pelo F12: conta, PIN do gerente, resultado e volta ao login
    Click (Find $window 'CloseCash')
    Answer '100,00'
    Authorize 'bruno' '730514'
    $result = Named 'OK' 10
    $closed = ($window.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
        ForEach-Object { $_.Current.Name }) -join ' '
    Click $result
    $store = Text (Find $window 'StoreName' 15)
    if ($closed -notmatch 'Esperado: R\$ 100,00' -or $closed -notmatch 'Divergência: R\$ 0,00') { $failures += "fechamento: '$closed'" }
    elseif ($store -ne 'Dolce Affetto') { $failures += "fechamento: não voltou ao login ('$store')" }
    else { Write-Host 'ok  fechamento cego com o PIN do gerente e volta ao login' }

    # 7. ativação pela tela, contra uma retaguarda de mentira em localhost: o
    #    caixa grava no banco novo, reinicia sozinho e volta com a loja.
    Stop-Process -Id $process.Id -Force
    $process.WaitForExit(10000) | Out-Null
    $port = Get-Random -Minimum 20000 -Maximum 40000
    $requestFile = Join-Path $work 'activation-request.txt'
    $stub = Start-Job -ArgumentList $port, $requestFile -ScriptBlock {
        param($port, $requestFile)
        $listener = New-Object System.Net.HttpListener
        $listener.Prefixes.Add("http://localhost:$port/")
        $listener.Start()
        try {
            $context = $listener.GetContext()
            $reader = New-Object System.IO.StreamReader($context.Request.InputStream, [System.Text.Encoding]::UTF8)
            [System.IO.File]::WriteAllText($requestFile, $context.Request.HttpMethod + ' ' + $context.Request.Url.AbsolutePath + "`n" + $reader.ReadToEnd())
            $json = '{"tenant_id":"aaaaaaaa-0000-0000-0000-000000000001","store_id":"bbbbbbbb-0000-0000-0000-000000000002",' +
                    '"device_id":"cccccccc-0000-0000-0000-000000000003","sync_token":"token-do-smoke",' +
                    '"store_name":"Pool Bar","cloud_base_url":"http://localhost:' + $port + '/api"}'
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
            $context.Response.ContentType = 'application/json'
            $context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
            $context.Response.Close()
            # Parar o listener na hora aborta a resposta ainda no buffer do
            # http.sys, e o PDV veria "conexão cancelada pelo host remoto".
            Start-Sleep -Seconds 3
        } finally { $listener.Stop() }
    }
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $up = $false
        try { $probe = New-Object System.Net.Sockets.TcpClient('127.0.0.1', $port); $probe.Close(); $up = $true } catch { Start-Sleep -Milliseconds 200 }
    } while (-not $up -and (Get-Date) -lt $deadline)
    if (-not $up) { throw "A retaguarda de mentira não subiu na porta ${port}: $((Receive-Job $stub 2>&1) -join ' ')" }

    $process = Start-Process -FilePath $Exe -PassThru
    $window = WaitWindow $process
    Click (Find $window 'Activate')
    (Find $window 'Server').GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue("http://localhost:$port")
    (Find $window 'Code').GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('abcd-efgh-jkmn')
    Click (Find $window 'ActivateNow')
    $nameCondition = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Reiniciar')
    $deadline = (Get-Date).AddSeconds(25)
    do { $restart = $window.FindFirst($Tree::Descendants, $nameCondition); Start-Sleep -Milliseconds 200 } while (-not $restart -and (Get-Date) -lt $deadline)
    if (-not $restart) {
        $why = ((Find $window 'ActivationError' 2).FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name }) -join ' '
        throw "A ativação não concluiu: '$why'"
    }
    $old = $process.Id
    Click $restart
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $next = Get-Process -Name PDV -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $old -and $_.Path -eq (Resolve-Path $Exe).Path } | Select-Object -First 1
        Start-Sleep -Milliseconds 300
    } while (-not $next -and (Get-Date) -lt $deadline)
    if (-not $next) { throw 'O PDV não reiniciou depois da ativação.' }
    $process = $next
    $window = WaitWindow $process
    $store = Text (Find $window 'StoreName' 20)
    $request = if (Test-Path $requestFile) { Get-Content $requestFile -Raw -Encoding UTF8 } else { '' }
    $archived = @(Get-ChildItem $work -Filter 'pdv_demo-*.db')
    if ($store -ne 'Pool Bar') { $failures += "ativação: a loja depois do reinício é '$store'" }
    elseif ($request -notmatch '^POST /api/devices/activate' -or $request -notmatch '"activation_code":"ABCDEFGHJKMN"') { $failures += "ativação: pedido inesperado '$request'" }
    elseif ($archived.Count -ne 1) { $failures += "ativação: esperado 1 banco de demonstração arquivado, há $($archived.Count)" }
    elseif ($window.FindFirst($Tree::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, 'Activate')))) { $failures += 'ativação: o caixa ativado ainda oferece ativar' }
    else { Write-Host 'ok  ativação pela tela, reinício e demonstração arquivada' }
}
catch {
    $failures += $_.Exception.Message
    $failures += Diagnose
}
finally {
    Get-Process -Name PDV -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Resolve-Path $Exe).Path } | Stop-Process -Force
    if ($stub) { Stop-Job $stub -ErrorAction SilentlyContinue; Remove-Job $stub -Force -ErrorAction SilentlyContinue }
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
Write-Host 'Caixa: ok (login, abertura, venda, TEF, cliente, pré-pago, balança, desconto, cancelamento, fechamento e ativação)'
