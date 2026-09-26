<#
.SYNOPSIS
    Abre o PDV em C# com um banco de demonstração, para testar à mão.

.DESCRIPTION
    Enquanto a C7a não chega, o banco nasce pelo migrate() e pela demonstração
    do PDV em Python (data/seed.py): produtos, insumos, mesas, garçons e os
    PINs de demonstração. O banco fica em %LOCALAPPDATA%\PDV-teste e é
    reaproveitado entre execuções; -Reset começa do zero.

    Nada aqui toca o banco da loja (C:\ProgramData\ERPFood\PDV).

.EXAMPLE
    .\tools\run-demo.ps1
    .\tools\run-demo.ps1 -Reset
    .\tools\run-demo.ps1 -NoBuild
#>
[CmdletBinding()]
param([switch] $Reset, [switch] $NoBuild)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Resolve-Path (Join-Path $here '..\..\..')
$project = Join-Path $here '..\src\Pdv.WinUI\Pdv.WinUI.csproj'
$exe = Join-Path $here '..\src\Pdv.WinUI\bin\x64\Debug\net10.0-windows10.0.19041.0\win-x64\PDV.exe'

if (-not $NoBuild) {
    Write-Host 'Compilando o PDV em C#...'
    & dotnet build $project -v q -nologo
    if ($LASTEXITCODE -ne 0) { throw 'A compilação falhou.' }
}
if (-not (Test-Path $exe)) { throw "PDV.exe não existe em '$exe'. Rode sem -NoBuild." }

$work = Join-Path $env:LOCALAPPDATA 'PDV-teste'
$db = Join-Path $work 'pdv_local.db'
if ($Reset -and (Test-Path $work)) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force $work | Out-Null

if (-not (Test-Path $db)) {
    Write-Host "Criando o banco de demonstração em $db ..."
    $env:PYTHONPATH = Join-Path $root 'apps\desktop-pdv\src'
    $seed = @"
from pathlib import Path
from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.seed import seed_demo_data
config = AppConfig.from_env()
db = Database(Path(r'$db')); db.migrate()
seed_demo_data(db, config)
with db.transaction() as c:
    c.execute("INSERT OR IGNORE INTO device_settings (key, value, updated_at) VALUES ('store.name', 'Confeitaria Demo', 'x')")
db.close()
"@
    # Por arquivo: o PowerShell 5.1 come as aspas ao passar argumento a programa nativo.
    $seedFile = Join-Path $work 'seed.py'
    Set-Content -Path $seedFile -Value $seed -Encoding UTF8
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & python $seedFile 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    Remove-Item $seedFile -ErrorAction SilentlyContinue
    if ($code -ne 0) {
        Remove-Item $db -ErrorAction SilentlyContinue
        throw ("O Python não conseguiu criar o banco: " + (($output | Select-Object -Last 3) -join ' | '))
    }
}

$env:PDV_DB_PATH = $db
$env:PDV_CRASH_LOG = Join-Path $work 'pdv-winui.log'

Write-Host ''
Write-Host 'Logins da demonstração:'
Write-Host '  caixa    ana     PIN 705284'
Write-Host '  gerente  bruno   PIN 483916'
Write-Host '  dona     olivia  PIN 84627519'
Write-Host '  garçom   joao    PIN 629471 (no app do garçom, no celular)'
Write-Host ''
Write-Host "Banco: $db"
Write-Host "Log de erro: $env:PDV_CRASH_LOG"
Start-Process -FilePath (Resolve-Path $exe)
