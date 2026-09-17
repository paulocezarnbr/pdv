; ===========================================================================
;  Instalador do PDV de Balcao - ERP Food Service
;
;  Compilar:  ISCC.exe packaging\installer.iss
;  Requer:    Inno Setup 6.2+  (https://jrsoftware.org/isdl.php)
;             build do PyInstaller ja feito em dist\PDV\
;
;  Modelo de permissao adotado:
;    Program Files  -> Administradores/SYSTEM: total | Usuarios: ler+executar
;    ProgramData    -> Usuarios: modificar (o app grava a venda como usuario)
;    logs           -> Usuarios: append-only (nao pode apagar rastro)
; ===========================================================================

#define AppName        "PDV Balcao"
#define AppVersion     "1.0.0"
#define AppPublisher   "ERP Food Service"
#define AppExeName     "PDV.exe"
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
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=none

; O PDV so faz sentido em 64 bits; limitar evita instalacao em maquina errada.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Impede downgrade silencioso: reinstalar versao antiga por cima e uma forma
; conhecida de reintroduzir vulnerabilidade ja corrigida.
VersionInfoVersion={#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; GroupDescription: "Atalhos:"
Name: "autostart";  Description: "Iniciar o PDV junto com o Windows"; GroupDescription: "Inicializacao:"
Name: "firewall";   Description: "Liberar a porta do servidor local (app do garcom na rede da loja)"; GroupDescription: "Rede:"

[Files]
; Todo o build onedir do PyInstaller.
Source: "..\dist\PDV\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Scripts de manutencao ficam no diretorio protegido: o operador nao pode
; edita-los para desfazer o proprio endurecimento.
Source: "harden.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[Dirs]
; Criados com ACL explicita logo depois, pelo harden.ps1.
Name: "{#DataDir}"
Name: "{#DataDir}\logs"
Name: "{#DataDir}\cupons"

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"
Name: "{group}\Desinstalar {#AppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{commonstartup}\{#AppName}";      Filename: "{app}\{#AppExeName}"; Tasks: autostart

[Run]
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

; --- 3. Primeira execucao -------------------------------------------------
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
  O banco de dados da loja NUNCA e removido na desinstalacao.

  Uma reinstalacao e operacao rotineira de suporte; apagar vendas ainda nao
  sincronizadas junto com o programa destruiria o faturamento do dia. Os dados
  ficam em ProgramData e sobrevivem — a remocao e sempre ato deliberado e
  manual do administrador.
  --------------------------------------------------------------------------- }

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

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
