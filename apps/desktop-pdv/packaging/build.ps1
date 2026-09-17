<#
.SYNOPSIS
    Pipeline de build do PDV: binario -> assinatura -> instalador.

.PARAMETER Backend
    pyinstaller (padrao) - empacota bytecode .pyc dentro do .exe.
                           Rapido de compilar. O bytecode E EXTRAIVEL.
    nuitka               - compila para C e depois para codigo de maquina.
                           Nao ha .pyc para extrair. Compilacao lenta (10-25 min).
                           Esta e a opcao correta quando proteger a logica de
                           negocio importa de verdade.

.PARAMETER SignCert
    Caminho do .pfx de Assinatura de Codigo. Sem assinatura o SmartScreen do
    Windows exibe "Editor desconhecido" e parte dos clientes nao conclui a
    instalacao. Alem disso, binario assinado permite detectar adulteracao do
    proprio executavel apos instalado.

.EXAMPLE
    .\packaging\build.ps1
    .\packaging\build.ps1 -Backend nuitka -SignCert .\cert.pfx
#>

[CmdletBinding()]
param(
    [ValidateSet('pyinstaller', 'nuitka')]
    [string] $Backend = 'pyinstaller',

    [string] $SignCert,
    [string] $SignPassword,
    [string] $TimestampUrl = 'http://timestamp.digicert.com',
    [switch] $SkipInstaller
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "== Build do PDV ($Backend) ==" -ForegroundColor Cyan

    # -- 0. Os testes sao porta de entrada, nao etapa opcional ---------------
    Write-Host "`n[0/4] Rodando os testes..."
    $env:PYTHONPATH = 'src'
    & python -m pytest tests/ -q
    if ($LASTEXITCODE -ne 0) {
        throw 'Testes falharam. Build abortado - nao se empacota codigo quebrado.'
    }

    # -- 1. Limpeza ----------------------------------------------------------
    Write-Host "`n[1/4] Limpando artefatos anteriores..."
    foreach ($dir in @('build', 'dist')) {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    }

    # -- 2. Compilacao -------------------------------------------------------
    Write-Host "`n[2/4] Compilando com $Backend..."

    if ($Backend -eq 'pyinstaller') {
        & python -m PyInstaller packaging/pdv.spec --noconfirm --clean
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller falhou.' }
        $exePath = 'dist\PDV\PDV.exe'
    }
    else {
        # Nuitka traduz o Python para C e compila nativamente. O resultado nao
        # contem bytecode: nao ha .pyc para extrair nem desmontar. Recuperar a
        # logica exige engenharia reversa de binario nativo — outra ordem de
        # grandeza de esforco.
        & python -m nuitka `
            --standalone `
            --assume-yes-for-downloads `
            --enable-plugin=pyside6 `
            --windows-console-mode=disable `
            --include-data-files=src/pdv/data/schema.sql=pdv/data/schema.sql `
            --include-package=pdv `
            --output-dir=dist `
            --output-filename=PDV.exe `
            --company-name="ERP Food Service" `
            --product-name="PDV Balcao" `
            --file-version=1.0.0.0 `
            --product-version=1.0.0.0 `
            main.py
        if ($LASTEXITCODE -ne 0) { throw 'Nuitka falhou.' }

        if (Test-Path 'dist\main.dist') {
            Rename-Item 'dist\main.dist' 'PDV'
        }
        $exePath = 'dist\PDV\PDV.exe'
    }

    if (-not (Test-Path $exePath)) { throw "Binario nao encontrado em $exePath" }
    $sizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 2)
    Write-Host "      OK - $exePath ($sizeMb MB)"

    # -- 3. Assinatura -------------------------------------------------------
    if ($SignCert) {
        Write-Host "`n[3/4] Assinando o binario..."
        $signtool = Get-ChildItem `
            'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe' `
            -ErrorAction SilentlyContinue | Select-Object -Last 1

        if (-not $signtool) { throw 'signtool.exe nao encontrado (instale o Windows SDK).' }

        $signArgs = @('sign', '/fd', 'SHA256', '/f', $SignCert)
        if ($SignPassword) { $signArgs += @('/p', $SignPassword) }
        # Carimbo de tempo: sem ele a assinatura expira junto com o certificado
        # e o binario ja instalado passa a acusar erro.
        $signArgs += @('/tr', $TimestampUrl, '/td', 'SHA256', $exePath)

        & $signtool.FullName @signArgs
        if ($LASTEXITCODE -ne 0) { throw 'Assinatura falhou.' }
        Write-Host '      OK - binario assinado e com carimbo de tempo.'
    }
    else {
        Write-Host "`n[3/4] Assinatura ignorada (sem -SignCert)." -ForegroundColor Yellow
        Write-Host '      AVISO: o SmartScreen exibira "Editor desconhecido".' -ForegroundColor Yellow
    }

    # -- 4. Instalador -------------------------------------------------------
    if (-not $SkipInstaller) {
        Write-Host "`n[4/4] Gerando o instalador..."
        $iscc = @(
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
            'C:\Program Files\Inno Setup 6\ISCC.exe'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1

        if (-not $iscc) {
            Write-Warning '      Inno Setup nao encontrado - baixe em https://jrsoftware.org/isdl.php'
        }
        else {
            & $iscc 'packaging\installer.iss'
            if ($LASTEXITCODE -ne 0) { throw 'Inno Setup falhou.' }

            $setup = Get-ChildItem 'dist\installer\*.exe' | Select-Object -First 1
            if ($SignCert -and $setup) {
                # O instalador tambem e assinado: e ele que o cliente baixa e
                # executa com privilegio de administrador.
                $signtool = Get-ChildItem `
                    'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe' |
                    Select-Object -Last 1
                & $signtool.FullName sign /fd SHA256 /f $SignCert `
                    /tr $TimestampUrl /td SHA256 $setup.FullName
            }
            Write-Host "      OK - $($setup.FullName)"
        }
    }

    Write-Host "`nBuild concluido." -ForegroundColor Green
}
finally {
    Pop-Location
}
