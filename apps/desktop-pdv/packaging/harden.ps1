<#
.SYNOPSIS
    Endurece as permissoes NTFS da instalacao do PDV.

.DESCRIPTION
    Executado pelo instalador (com privilegio de administrador) logo apos a
    copia dos arquivos. Separa o que e PROGRAMA do que e DADO, porque os dois
    tem exigencias opostas:

      * Programa (Program Files): o operador precisa APENAS executar. Escrita
        negada bloqueia troca de DLL, de .pyc e do proprio .exe.
      * Dado (ProgramData): o app roda como usuario comum e PRECISA gravar a
        venda. Escrita nao pode ser negada sem quebrar o caixa.

    Essa assimetria e inerente ao modelo "app desktop rodando como usuario".
    A mitigacao para o banco esta documentada em packaging/README.md
    (servico Windows como LocalSystem + IPC).

.NOTES
    Exige elevacao. Sem privilegio de administrador, icacls falha silenciosamente
    em parte dos casos — por isso a verificacao no fim nao e opcional.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $InstallDir,

    [Parameter(Mandatory = $true)]
    [string] $DataDir,

    [switch] $EnableAuditing
)

$ErrorActionPreference = 'Stop'

# SIDs bem-conhecidos em vez de nomes: "Users"/"Usuarios" muda com o idioma do
# Windows, e um instalador que so funciona em Windows em ingles e um chamado
# de suporte garantido.
$SID_SYSTEM         = '*S-1-5-18'   # NT AUTHORITY\SYSTEM
$SID_ADMINISTRATORS = '*S-1-5-32-544'
$SID_USERS          = '*S-1-5-32-545'

function Assert-Elevated {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Este script precisa ser executado como Administrador.'
    }
}

function Invoke-Icacls {
    param([string[]] $Arguments)
    $output = & icacls.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls falhou ($LASTEXITCODE): $output"
    }
    return $output
}

function Protect-ProgramDirectory {
    <#
        Programa: somente Administradores e SYSTEM modificam.
        Usuarios recebem apenas leitura + execucao.
    #>
    param([string] $Path)

    Write-Host "[1/4] Protegendo o diretorio do programa: $Path"

    # /inheritance:r remove as ACEs herdadas. Sem isso, uma permissao ampla
    # herdada da raiz continuaria valendo e o endurecimento seria decorativo.
    Invoke-Icacls @($Path, '/inheritance:r', '/Q') | Out-Null

    Invoke-Icacls @(
        $Path,
        "/grant:r", "${SID_SYSTEM}:(OI)(CI)(F)",
        "/grant:r", "${SID_ADMINISTRATORS}:(OI)(CI)(F)",
        # RX = Read & Execute. Note a AUSENCIA de (W)/(M): o operador executa,
        # mas nao troca nenhum arquivo do programa.
        "/grant:r", "${SID_USERS}:(OI)(CI)(RX)",
        '/T', '/C', '/Q'
    ) | Out-Null

    # Dono explicito: se o diretorio ficasse com o instalador como dono, um
    # usuario "dono" poderia reescrever a propria ACL (WRITE_DAC implicito).
    #
    # Pelo SID, como as concessoes acima. O nome "Administrators" so existe no
    # Windows em ingles; no portugues o grupo e "Administradores", o icacls
    # falhava, o script parava AQUI - antes de liberar a pasta de dados - e o
    # caixa abria com "attempt to write a readonly database".
    Invoke-Icacls @($Path, '/setowner', $SID_ADMINISTRATORS, '/T', '/C', '/Q') | Out-Null

    Write-Host '      OK - usuarios sem permissao de escrita no programa.'
}

function Protect-DataDirectory {
    <#
        Dados: o app roda como usuario comum e PRECISA gravar a venda.
        Concedemos Modify, mas NAO Full Control — a diferenca importa:
        Full daria WRITE_DAC/WRITE_OWNER, permitindo ao operador reescrever a
        propria ACL e, por exemplo, se conceder permissao sobre os logs.
    #>
    param([string] $Path)

    Write-Host "[2/4] Configurando o diretorio de dados: $Path"

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }

    Invoke-Icacls @($Path, '/inheritance:r', '/Q') | Out-Null
    Invoke-Icacls @(
        $Path,
        "/grant:r", "${SID_SYSTEM}:(OI)(CI)(F)",
        "/grant:r", "${SID_ADMINISTRATORS}:(OI)(CI)(F)",
        "/grant:r", "${SID_USERS}:(OI)(CI)(M)",
        '/T', '/C', '/Q'
    ) | Out-Null

    Write-Host '      OK - gravavel pelo operador (necessario para vender).'
    Write-Host '      AVISO: o operador consegue abrir o .db com um editor de SQLite.'
    Write-Host '             Deteccao: cadeia HMAC + ancoragem no servidor.'
}

function Protect-LogDirectory {
    <#
        Logs: o app ACRESCENTA, mas nao pode APAGAR nem REESCREVER.
        Isto e append-only de verdade no NTFS:
          WD = escrever dados  (negado)
          AD = acrescentar dados (concedido)
        Um operador que queira sumir com o rastro nao consegue truncar o log.
    #>
    param([string] $Path)

    Write-Host "[3/4] Tornando os logs append-only: $Path"

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }

    Invoke-Icacls @($Path, '/inheritance:r', '/Q') | Out-Null
    Invoke-Icacls @(
        $Path,
        "/grant:r", "${SID_SYSTEM}:(OI)(CI)(F)",
        "/grant:r", "${SID_ADMINISTRATORS}:(OI)(CI)(F)",
        # Permite criar arquivo e acrescentar; nega delete e sobrescrita.
        "/grant:r", "${SID_USERS}:(OI)(CI)(AD,REA,RA,S,RC)",
        '/T', '/C', '/Q'
    ) | Out-Null

    Write-Host '      OK - log pode crescer, nao pode ser apagado pelo operador.'
}

function Enable-DatabaseAuditing {
    <#
        SACL no banco: o Windows passa a registrar no Log de Seguranca QUEM
        abriu o arquivo para escrita e QUANDO — inclusive um administrador.

        Este e o controle que fecha a lacuna do "admin local pode tudo": ele
        nao impede o acesso, mas deixa rastro fora do alcance do app. Combinado
        com o encaminhamento do Event Log para um coletor remoto, o rastro sai
        da maquina e vira prova.
    #>
    param([string] $DatabasePath)

    Write-Host "[4/4] Ativando auditoria de acesso ao banco: $DatabasePath"

    # A politica de auditoria precisa estar ligada, senao a SACL nao gera evento.
    # Pelo GUID da subcategoria "Sistema de Arquivos": o nome e traduzido
    # conforme o idioma do Windows, e "File System" nao existe no portugues.
    $null = & auditpol.exe /set /subcategory:"{0CCE921D-69AE-11D9-BED3-505054503030}" /success:enable /failure:enable 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning '      auditpol falhou; a SACL sera gravada mas nao gerara eventos.'
    }

    if (-not (Test-Path -LiteralPath $DatabasePath)) {
        New-Item -ItemType File -Path $DatabasePath -Force | Out-Null
    }

    $acl  = Get-Acl -LiteralPath $DatabasePath -Audit
    $rule = New-Object System.Security.AccessControl.FileSystemAuditRule(
        [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0'),  # Todos
        'Write, Delete, ChangePermissions, TakeOwnership',
        'Success, Failure'
    )
    $acl.AddAuditRule($rule)
    Set-Acl -LiteralPath $DatabasePath -AclObject $acl

    Write-Host '      OK - gravacoes no banco viram evento 4663 no Log de Seguranca.'
}

function Test-Hardening {
    <#
        Verificacao explicita. Um instalador que "parece" ter endurecido as
        permissoes e pior que um que nao tentou: cria confianca sem base.
    #>
    param([string] $InstallPath, [string] $DataPath)

    Write-Host ''
    Write-Host 'Verificacao final:'
    $acl = Get-Acl -LiteralPath $InstallPath

    $writableByUsers = $acl.Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-32-545' -and
        $_.AccessControlType -eq 'Allow' -and
        ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Write)
    }

    if ($writableByUsers) {
        throw "FALHA: o grupo Usuarios ainda tem escrita em $InstallPath"
    }

    Write-Host '  OK - grupo Usuarios sem escrita no diretorio do programa.'
    Write-Host "  Dono: $($acl.Owner)"

    # O outro lado da assimetria: sem escrita na pasta de dados o caixa nem abre
    # ("attempt to write a readonly database"). Foi exatamente o que passou
    # despercebido quando o script parava antes de chegar aqui.
    $dataAcl = Get-Acl -LiteralPath $DataPath
    $usersCanWrite = $dataAcl.Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-32-545' -and
        $_.AccessControlType -eq 'Allow' -and
        ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Modify) -eq [System.Security.AccessControl.FileSystemRights]::Modify
    }
    if (-not $usersCanWrite) {
        throw "FALHA: o grupo Usuarios nao consegue gravar em $DataPath - o caixa nao abriria"
    }
    Write-Host '  OK - grupo Usuarios grava na pasta de dados.'
}

# ---------------------------------------------------------------------------
Assert-Elevated

Protect-ProgramDirectory -Path $InstallDir
Protect-DataDirectory   -Path $DataDir
Protect-LogDirectory    -Path (Join-Path $DataDir 'logs')

if ($EnableAuditing) {
    Enable-DatabaseAuditing -DatabasePath (Join-Path $DataDir 'pdv_local.db')
}

Test-Hardening -InstallPath $InstallDir -DataPath $DataDir

Write-Host ''
Write-Host 'Endurecimento concluido.'
