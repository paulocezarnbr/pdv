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

.PARAMETER RequireInstaller
    Falha se o Inno Setup nao estiver instalado, em vez de so avisar. O CI
    liga isto: um build "verde" sem o instalador entregaria o binario solto,
    sem ACL, sem VC++ e sem o assistente de instalacao.

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
    [switch] $SkipInstaller,
    [switch] $RequireInstaller
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    Write-Host "== Build do PDV ($Backend) ==" -ForegroundColor Cyan

    # -- 0. Os testes sao porta de entrada, nao etapa opcional ---------------
    Write-Host "`n[0/5] Rodando os testes..."
    $env:PYTHONPATH = 'src'
    & python -m pytest tests/ -q
    if ($LASTEXITCODE -ne 0) {
        throw 'Testes falharam. Build abortado - nao se empacota codigo quebrado.'
    }

    # -- 1. Limpeza ----------------------------------------------------------
    Write-Host "`n[1/5] Limpando artefatos anteriores..."
    foreach ($dir in @('build', 'dist')) {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    }

    # -- 2. Compilacao -------------------------------------------------------
    Write-Host "`n[2/5] Compilando com $Backend..."

    if ($Backend -eq 'pyinstaller') {
        # O spec produz DOIS executaveis no mesmo diretorio: PDV.exe (o caixa) e
        # PDVSetup.exe (o assistente chamado pelo instalador).
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
            --file-version=1.1.0.0 `
            --product-version=1.1.0.0 `
            main.py
        if ($LASTEXITCODE -ne 0) { throw 'Nuitka falhou.' }

        if (Test-Path 'dist\main.dist') {
            Rename-Item 'dist\main.dist' 'PDV'
        }
        $exePath = 'dist\PDV\PDV.exe'

        # O assistente de instalacao precisa ser compilado a parte: o Nuitka
        # gera um executavel por ponto de entrada. Sem esta etapa o instalador
        # apontaria para um PDVSetup.exe inexistente e a instalacao terminaria
        # com o caixa sem banco, sem segredo e sem perifericos configurados.
        Write-Host '      Compilando o assistente de instalacao...'
        & python -m nuitka `
            --standalone `
            --assume-yes-for-downloads `
            --enable-plugin=pyside6 `
            --windows-console-mode=disable `
            --include-data-files=src/pdv/data/schema.sql=pdv/data/schema.sql `
            --include-package=pdv `
            --output-dir=dist `
            --output-filename=PDVSetup.exe `
            --company-name="ERP Food Service" `
            --product-name="PDV Balcao - Instalacao" `
            --file-version=1.1.0.0 `
            --product-version=1.1.0.0 `
            setup_wizard.py
        if ($LASTEXITCODE -ne 0) { throw 'Nuitka falhou ao compilar o assistente.' }

        # As duas compilacoes partem das mesmas dependencias, entao so o
        # executavel e os poucos arquivos exclusivos precisam ser mesclados no
        # diretorio final. Duplicar as DLLs do Qt dobraria o tamanho a toa.
        $setupDist = 'dist\setup_wizard.dist'
        if (-not (Test-Path $setupDist)) { throw "Nuitka nao gerou $setupDist" }
        Get-ChildItem $setupDist -Recurse -File | ForEach-Object {
            $relative = $_.FullName.Substring((Resolve-Path $setupDist).Path.Length + 1)
            $target = Join-Path 'dist\PDV' $relative
            if (-not (Test-Path $target)) {
                $parent = Split-Path -Parent $target
                if (-not (Test-Path $parent)) {
                    New-Item -ItemType Directory -Force $parent | Out-Null
                }
                Copy-Item $_.FullName $target
            }
        }
        Remove-Item $setupDist -Recurse -Force
    }

    if (-not (Test-Path $exePath)) { throw "Binario nao encontrado em $exePath" }

    # O instalador chama os dois. Faltando um, a falha so apareceria na loja.
    $binaries = @($exePath, 'dist\PDV\PDVSetup.exe')
    foreach ($binary in $binaries) {
        if (-not (Test-Path $binary)) { throw "Binario nao encontrado em $binary" }
        $sizeMb = [math]::Round((Get-Item $binary).Length / 1MB, 2)
        Write-Host "      OK - $binary ($sizeMb MB)"
    }

    # -- 3. Autoteste DENTRO do pacote --------------------------------------
    #
    # A suite roda contra o codigo-fonte, onde todo arquivo esta no lugar e
    # todo modulo e importavel. O executavel empacotado e outro programa: o
    # PyInstaller monta a arvore de imports por analise estatica, e todo import
    # tardio e invisivel para ela. Este projeto esta cheio deles, cada um por
    # um bom motivo — e um `hiddenimports` incompleto produz um pacote que
    # instala, abre, e falha na loja.
    Write-Host "`n[3/5] Rodando o autoteste dentro do pacote..."
    # O PDV.exe e compilado sem console, entao o relatorio sai num arquivo ao
    # lado do binario — do contrario ele se perderia inteiro aqui.
    #
    # `Start-Process -Wait`, e nao `& $exePath`: o PDV.exe e um app GRAFICO, e o
    # PowerShell nao espera app grafico terminar nem preenche $LASTEXITCODE
    # com o codigo dele. Com `&`, o autoteste "passava" sem ter rodado — o
    # codigo lido era o do PyInstaller, e o relatorio ainda nem existia.
    $report = 'dist\PDV\selftest.log'
    if (Test-Path $report) { Remove-Item $report -Force }
    $selftest = Start-Process -FilePath $exePath -ArgumentList '--selftest' `
        -Wait -PassThru -NoNewWindow
    $selftestCode = $selftest.ExitCode
    if ($null -eq $selftestCode) {
        throw 'Autoteste sem codigo de saida: nao da para afirmar que o pacote esta completo.'
    }
    if (-not (Test-Path $report)) {
        throw 'Autoteste nao gerou relatorio: o PDV.exe nem chegou a rodar.'
    }
    Get-Content $report | ForEach-Object { "      $_" }
    # O relatorio e do build, nao da loja: nao vai dentro do instalador.
    Remove-Item $report -Force
    if ($selftestCode -ne 0) {
        throw "Autoteste falhou (codigo $selftestCode): o pacote esta incompleto. Nao publique."
    }

    # -- 3. Assinatura -------------------------------------------------------
    if ($SignCert) {
        Write-Host "`n[4/5] Assinando os binarios..."
        $signtool = Get-ChildItem `
            'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe' `
            -ErrorAction SilentlyContinue | Select-Object -Last 1

        if (-not $signtool) { throw 'signtool.exe nao encontrado (instale o Windows SDK).' }

        # Os dois sao assinados. Um PDVSetup.exe sem assinatura dispararia o
        # SmartScreen no meio da instalacao, depois de o cliente ja ter
        # aprovado o instalador — o momento em que ele mais desconfia.
        foreach ($binary in $binaries) {
            $signArgs = @('sign', '/fd', 'SHA256', '/f', $SignCert)
            if ($SignPassword) { $signArgs += @('/p', $SignPassword) }
            # Carimbo de tempo: sem ele a assinatura expira junto com o
            # certificado e o binario ja instalado passa a acusar erro.
            $signArgs += @('/tr', $TimestampUrl, '/td', 'SHA256', $binary)

            & $signtool.FullName @signArgs
            if ($LASTEXITCODE -ne 0) { throw "Assinatura de $binary falhou." }
        }
        Write-Host '      OK - binarios assinados e com carimbo de tempo.'
    }
    else {
        Write-Host "`n[4/5] Assinatura ignorada (sem -SignCert)." -ForegroundColor Yellow
        Write-Host '      AVISO: o SmartScreen exibira "Editor desconhecido".' -ForegroundColor Yellow
    }

    # -- 4. Instalador -------------------------------------------------------
    if (-not $SkipInstaller) {
        Write-Host "`n[5/5] Gerando o instalador..."
        $iscc = @(
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
            'C:\Program Files\Inno Setup 6\ISCC.exe'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1

        if (-not $iscc) {
            if ($RequireInstaller) { throw 'Inno Setup nao encontrado.' }
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
                $signArgs = @('sign', '/fd', 'SHA256', '/f', $SignCert)
                if ($SignPassword) { $signArgs += @('/p', $SignPassword) }
                $signArgs += @('/tr', $TimestampUrl, '/td', 'SHA256', $setup.FullName)
                & $signtool.FullName @signArgs
                if ($LASTEXITCODE -ne 0) { throw 'Assinatura do instalador falhou.' }
            }
            Write-Host "      OK - $($setup.FullName)"
        }
    }

    Write-Host "`nBuild concluido." -ForegroundColor Green
}
finally {
    Pop-Location
}
