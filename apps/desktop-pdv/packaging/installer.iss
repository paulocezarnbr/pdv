; ===========================================================================
;  Instalador do PDV de Balcao - ERP Food Service
;
;  Compilar:  ISCC.exe packaging\installer.iss
;  Requer:    Inno Setup 6.2+  (https://jrsoftware.org/isdl.php)
;             build do PyInstaller ja feito em dist\PDV\
;             packaging\redist\VC_redist.x64.exe  (ver redist\README.md)
;
;  Instalador AUTOSSUFICIENTE: um unico .exe, sem pre-requisito manual.
;  O runtime Python e todas as bibliotecas ja vem embarcados no build do
;  PyInstaller - o lojista NAO instala Python nem roda pip.
;
;  Modelo de permissao adotado:
;    Program Files  -> Administradores/SYSTEM: total | Usuarios: ler+executar
;    ProgramData    -> Usuarios: modificar (o app grava a venda como usuario)
;    logs           -> Usuarios: append-only (nao pode apagar rastro)
; ===========================================================================

#define AppName        "PDV Balcao"
#define AppVersion     "1.1.0"
#define AppPublisher   "ERP Food Service"
#define AppExeName     "PDV.exe"
#define SetupExeName   "PDVSetup.exe"
#define AppId          "{{8F3A6C21-4E7B-4D19-9A2F-5C8E1B7D3A64}"
#define DataDir        "{commonappdata}\ERPFood\PDV"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\ERPFood\PDV
DefaultGroupName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename=PDV-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Exige elevacao: sem ela nao ha como gravar em Program Files nem aplicar ACL.
; `PrivilegesRequiredOverridesAllowed` fica AUSENTE de proposito: ausente, nem a
; linha de comando nem o assistente podem trocar para instalacao sem admin. O
; Inno so aceita `commandline`/`dialog` ali - `none` nao existe, e com ele o
; compilador abortava.
PrivilegesRequired=admin

; O PDV so faz sentido em 64 bits; limitar evita instalacao em maquina errada.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Impede downgrade silencioso: reinstalar versao antiga por cima e uma forma
; conhecida de reintroduzir vulnerabilidade ja corrigida.
VersionInfoVersion={#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes

; --- Atualizacao no lugar -------------------------------------------------
; O PDV aberto mantem PDV.exe e as DLLs do Qt travados. Sem fechar antes, o
; Inno grava parte dos arquivos, falha no resto e exige reinicio - deixando a
; loja com um diretorio meio atualizado ate alguem reiniciar a maquina.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
; Nao reabrimos sozinhos: quem decide quando o caixa volta a operar e o lojista,
; e reabrir no meio de uma conferencia atrapalha mais do que ajuda.
RestartApplications=no
; Dados, fila de sincronizacao e banco vivem em ProgramData, fora de {app}.
; A atualizacao troca binario e nada mais.
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; GroupDescription: "Atalhos:"
Name: "autostart";  Description: "Iniciar o PDV junto com o Windows"; GroupDescription: "Inicializacao:"
Name: "firewall";   Description: "Liberar a porta do servidor local (app do garcom na rede da loja)"; GroupDescription: "Rede:"

[Files]
; Todo o build onedir do PyInstaller: ja inclui o interpretador Python, o Qt
; e cada biblioteca de terceiros. Nada e baixado durante a instalacao.
Source: "..\dist\PDV\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Scripts de manutencao ficam no diretorio protegido: o operador nao pode
; edita-los para desfazer o proprio endurecimento.
Source: "harden.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

; --- Pre-requisito embarcado ----------------------------------------------
; O Qt depende do Visual C++ Runtime. Numa maquina recem-formatada ele NAO
; existe, e sem ele o PDV.exe encerra sem mensagem nenhuma - o sintoma classico
; de "instalei e nao abre". Embarcar (em vez de baixar na hora) garante a
; implantacao tambem em loja que ainda nao tem internet configurada.
Source: "redist\VC_redist.x64.exe"; DestDir: "{tmp}"; \
    Flags: deleteafterinstall; Check: not IsVCRedistInstalled

[Dirs]
; Criados com ACL explicita logo depois, pelo harden.ps1.
Name: "{#DataDir}"
Name: "{#DataDir}\logs"
Name: "{#DataDir}\cupons"

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"
; Atalho de suporte: o tecnico troca a balanca ou o cabo USB muda de porta e
; redetecta sem reinstalar nada. Sem isto, a alternativa no balcao seria editar
; o banco a mao.
Name: "{group}\Reconfigurar perifericos"; Filename: "{app}\{#SetupExeName}"; \
    Parameters: "--detect-only --data-dir ""{#DataDir}"""
Name: "{group}\Desinstalar {#AppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{commonstartup}\{#AppName}";      Filename: "{app}\{#AppExeName}"; Tasks: autostart

[Run]
; --- 0. Pre-requisito: Visual C++ Runtime ---------------------------------
Filename: "{tmp}\VC_redist.x64.exe"; \
    Parameters: "/install /quiet /norestart"; \
    StatusMsg: "Instalando componentes do Windows (Visual C++)..."; \
    Check: not IsVCRedistInstalled; \
    Flags: waituntilterminated

; --- 1. Endurecimento das permissoes NTFS ---------------------------------
; Roda ANTES de oferecer a execucao do app: se falhar, o instalador avisa e o
; administrador decide. Instalar sem ACL correta e pior que nao instalar.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\harden.ps1"" -InstallDir ""{app}"" -DataDir ""{#DataDir}"" -EnableAuditing"; \
    StatusMsg: "Aplicando permissoes de seguranca..."; \
    Flags: runhidden waituntilterminated

; --- 2. Regra de firewall para o servidor local (app do garcom) -----------
Filename: "netsh.exe"; \
    Parameters: "advfirewall firewall add rule name=""PDV Balcao - Servidor Local"" dir=in action=allow program=""{app}\{#AppExeName}"" protocol=TCP localport=8420 profile=private"; \
    StatusMsg: "Liberando a porta do servidor local..."; \
    Flags: runhidden waituntilterminated; Tasks: firewall

; --- 3. Provisionamento: banco, segredo do terminal e perifericos ---------
; Roda DEPOIS do harden.ps1, e nao antes: e ele que concede ao grupo Usuarios
; permissao de escrita em ProgramData. Invertida, a ordem criaria o banco com a
; ACL herdada e o endurecimento seguinte o deixaria inconsistente.
;
; Aqui o instalador deixa de "copiar arquivos" e passa a entregar um caixa que
; comprovadamente vende: cria o banco, gera o device_secret no DPAPI, varre as
; portas seriais atras da balanca, localiza a impressora termica e imprime um
; cupom de teste. O relatorio aparece na tela e fica em
; ProgramData\ERPFood\PDV\logs\setup.log.
;
; Codigo de saida 2 (pendencias) NAO aborta a instalacao: balanca desligada no
; momento da instalacao e rotina, e o proprio assistente explica o que fazer.
Filename: "{app}\{#SetupExeName}"; \
    Parameters: "--data-dir ""{#DataDir}"""; \
    StatusMsg: "Detectando balanca e impressora, testando a instalacao..."; \
    Flags: waituntilterminated; Check: not WizardSilent

; Instalacao automatizada (/SILENT): mesmo provisionamento, sem caixa de
; dialogo. O resultado vai so para o setup.log, que e o que o script de
; implantacao em massa consegue ler.
Filename: "{app}\{#SetupExeName}"; \
    Parameters: "--silent --data-dir ""{#DataDir}""{code:ActivationArg}"; \
    StatusMsg: "Detectando balanca e impressora..."; \
    Flags: runhidden waituntilterminated; Check: WizardSilent

; --- 4. Primeira execucao -------------------------------------------------
Filename: "{app}\{#AppExeName}"; \
    Description: "Abrir o {#AppName} agora"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh.exe"; \
    Parameters: "advfirewall firewall delete rule name=""PDV Balcao - Servidor Local"""; \
    Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
{ ---------------------------------------------------------------------------
  Deteccao do Visual C++ Runtime 2015-2022.

  Checar a chave de registro e mais confiavel que procurar o arquivo: a DLL
  pode existir em System32 vinda junto de outro programa, sem o runtime estar
  de fato registrado e completo.
  --------------------------------------------------------------------------- }

{ ---------------------------------------------------------------------------
  Bloqueio de downgrade.

  Reinstalar versao antiga por cima reintroduz vulnerabilidade ja corrigida e,
  pior neste sistema, pode rodar um binario que desconhece migrations ja
  aplicadas no banco da loja - com a fila de sincronizacao cheia de vendas em
  um esquema que ele nao entende.

  O caminho seguro de rollback e desinstalar (os dados ficam) e instalar a
  versao desejada, com decisao consciente de quem da suporte.
  --------------------------------------------------------------------------- }

function InstalledVersion(): String;
var
  Value: String;
  Key: String;
begin
  Result := '';
  Key := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1';

  { As duas visoes do registro sao consultadas de proposito. Em InitializeSetup
    o modo de instalacao 64 bits ainda nao esta decidido, entao HKLM sozinho
    poderia ler a visao errada e concluir "instalacao nova" onde ha uma
    instalacao anterior - justamente o caso em que a protecao de downgrade e o
    aviso de fechar o caixa mais importam. }
  if RegQueryStringValue(HKLM64, Key, 'DisplayVersion', Value) then
    Result := Value
  else if RegQueryStringValue(HKLM32, Key, 'DisplayVersion', Value) then
    Result := Value;
end;

function InitializeSetup(): Boolean;
var
  Existing: String;
  InstalledVer, ThisVer: Int64;
  Comparison: Integer;
begin
  Result := True;
  Existing := InstalledVersion();

  if Existing = '' then
    Exit;  { instalacao nova }

  { Versao gravada em formato inesperado: seguimos, mas sem prometer nada
    sobre a ordem - melhor atualizar do que travar a loja por um registro
    estranho. }
  if (not StrToVersion(Existing, InstalledVer)) or
     (not StrToVersion('{#AppVersion}', ThisVer)) then
    Exit;

  Comparison := ComparePackedVersion(InstalledVer, ThisVer);

  if Comparison > 0 then
  begin
    MsgBox('Versao mais recente ja instalada: ' + Existing + '.' + #13#10 + #13#10 +
           'Instalar a {#AppVersion} por cima seria um downgrade e pode deixar ' +
           'o banco da loja num formato que esta versao nao entende.' + #13#10 + #13#10 +
           'Para voltar de versao, desinstale primeiro (os dados sao mantidos).',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if Comparison = 0 then
  begin
    Result := (MsgBox('A versao {#AppVersion} ja esta instalada.' + #13#10 + #13#10 +
                      'Reinstalar por cima? Os dados da loja serao preservados.',
                      mbConfirmation, MB_YESNO) = IDYES);
    Exit;
  end;

  MsgBox('Atualizando o PDV de ' + Existing + ' para {#AppVersion}.' + #13#10 + #13#10 +
         'FECHE O CAIXA antes de continuar: o PDV aberto sera encerrado e uma ' +
         'venda em andamento seria perdida.' + #13#10 + #13#10 +
         'O banco de dados, a fila de sincronizacao e a ativacao do terminal ' +
         'sao preservados.',
         mbInformation, MB_OK);
end;

{ ---------------------------------------------------------------------------
  Codigo de ativacao para implantacao em massa.

      PDV-Setup-1.1.0.exe /SILENT /ACTIVATIONCODE=A1B2C3D4

  Na instalacao interativa isto fica vazio e quem pergunta e o proprio
  PDVSetup.exe, numa caixa de dialogo - o codigo e gerado no painel no momento
  da implantacao e ditado para quem esta na loja, entao o instalador nao teria
  como conhece-lo de antemao.

  Ativar nao e obrigatorio: sem codigo o PDV instala, vende e acumula na fila
  de saida. Travar a instalacao porque a internet da loja ainda nao foi ligada
  transformaria um contratempo em visita tecnica perdida.
  --------------------------------------------------------------------------- }

function ActivationArg(Param: String): String;
var
  Code: String;
begin
  Code := ExpandConstant('{param:ACTIVATIONCODE|}');
  if Code = '' then
    Result := ''
  else
    Result := ' --activation-code "' + Code + '"';
end;

function IsVCRedistInstalled(): Boolean;
var
  Installed: Cardinal;
begin
  { HKLM64 explicito: o runtime x64 registra-se na visao de 64 bits, e ler a
    visao de 32 bits concluiria "ausente" numa maquina que ja o tem - reinstalar
    por cima e inofensivo, mas custa tempo em toda implantacao. }
  Result := False;
  if RegQueryDWordValue(HKLM64,
       'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
       'Installed', Installed) then
    Result := (Installed = 1);
end;

{ ---------------------------------------------------------------------------
  O banco de dados da loja NUNCA e removido na desinstalacao.

  Uma reinstalacao e operacao rotineira de suporte; apagar vendas ainda nao
  sincronizadas junto com o programa destruiria o faturamento do dia. Os dados
  ficam em ProgramData e sobrevivem - a remocao e sempre ato deliberado e
  manual do administrador.
  --------------------------------------------------------------------------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    MsgBox('Os dados da loja (vendas, estoque e auditoria) foram MANTIDOS em:' + #13#10 +
           ExpandConstant('{#DataDir}') + #13#10 + #13#10 +
           'Remova essa pasta manualmente apenas se tiver certeza de que todas ' +
           'as vendas ja foram sincronizadas com a nuvem.',
           mbInformation, MB_OK);
  end;
end;
