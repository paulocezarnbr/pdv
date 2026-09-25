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
#define AppVersion     "1.1.6"
#define AppPublisher   "ERP Food Service"
#define AppExeName     "PDV.exe"
#define SetupExeName   "PDVSetup.exe"
#define AppGuid        "8F3A6C21-4E7B-4D19-9A2F-5C8E1B7D3A64"
; O "{{" e escape do [Setup] para UMA chave. No [Code] nao ha escape: la a
; chave do registro e montada com AppGuid (ver UninstallKey).
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
; Identidade visual gerada do tema pelo build (packaging/branding.py): o mesmo
; icone do PDV.exe, e as imagens em 100/150/200% para a tela nao borrar em
; notebook com escala. Sem isto o assistente era o cinza padrao do Inno.
SetupIconFile=assets\pdv.ico
WizardImageFile=assets\wizard-large-100.bmp,assets\wizard-large-150.bmp,assets\wizard-large-200.bmp
WizardSmallImageFile=assets\wizard-small-100.bmp,assets\wizard-small-150.bmp,assets\wizard-small-200.bmp
; Menos perguntas: a pasta do menu Iniciar nao muda nada para o lojista, e na
; atualizacao a pasta de instalacao ja esta decidida.
DisableProgramGroupPage=yes
DisableDirPage=auto

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

[Messages]
; A pagina final e onde o lojista decide se o PDV "funcionou". Sem dizer que
; o terminal nao ativado abre em demonstracao, a primeira coisa que ele ve e uma
; faixa amarela que parece erro.
brazilianportuguese.FinishedHeadingLabel=PDV Balcão instalado
brazilianportuguese.FinishedLabelNoIcons=O PDV Balcão está instalado neste computador.%n%nSe o terminal ainda não foi ativado, ele abre em modo demonstração: dá para experimentar tudo, e as vendas de teste ficam arquivadas quando você ativar pelo botão "Ativar terminal".
brazilianportuguese.FinishedLabel=O PDV Balcão está instalado neste computador.%n%nSe o terminal ainda não foi ativado, ele abre em modo demonstração: dá para experimentar tudo, e as vendas de teste ficam arquivadas quando você ativar pelo botão "Ativar terminal".

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"
Name: "autostart";  Description: "Iniciar o PDV junto com o Windows"; GroupDescription: "Inicialização:"
Name: "firewall";   Description: "Liberar a porta do servidor local (app do garçom na rede da loja)"; GroupDescription: "Rede:"

[Files]
; Todo o build onedir do PyInstaller: ja inclui o interpretador Python, o Qt
; e cada biblioteca de terceiros. Nada e baixado durante a instalacao.
Source: "..\dist\PDV\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
    Check: not QuickRepair

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
;
; `users-modify` e a rede de seguranca: o proprio Inno concede, pelo SID (vale
; em qualquer idioma do Windows) e sem depender do PowerShell. Se o harden.ps1
; nao rodar - politica de execucao, antivirus -, o caixa ainda grava a venda.
Name: "{#DataDir}"; Permissions: users-modify
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
;
; A saida vai para logs\harden.log e o AfterInstall confere se o script
; chegou ao fim. Antes o [Run] ignorava o codigo de saida: num Windows em
; portugues o script morria no primeiro passo (nome de grupo em ingles), a
; pasta de dados ficava sem escrita para o caixa, e o erro so aparecia ao abrir
; o PDV - "attempt to write a readonly database" - sem nada ligando uma coisa
; a outra.
Filename: "{cmd}"; \
    Parameters: "/c powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\harden.ps1"" -InstallDir ""{app}"" -DataDir ""{#DataDir}"" -EnableAuditing > ""{#DataDir}\logs\harden.log"" 2>&1"; \
    StatusMsg: "Aplicando permissões de segurança..."; \
    Flags: runhidden waituntilterminated; AfterInstall: CheckHardening

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
    Parameters: "--data-dir ""{#DataDir}""{code:ServerArg}"; \
    StatusMsg: "Detectando balança e impressora, testando a instalação..."; \
    Flags: waituntilterminated; Check: not WizardSilent

; Instalacao automatizada (/SILENT): mesmo provisionamento, sem caixa de
; dialogo. O resultado vai so para o setup.log, que e o que o script de
; implantacao em massa consegue ler.
Filename: "{app}\{#SetupExeName}"; \
    Parameters: "--silent --data-dir ""{#DataDir}""{code:ActivationArg}{code:ServerArg}"; \
    StatusMsg: "Detectando balança e impressora..."; \
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
  Conferencia do endurecimento.

  O harden.ps1 termina escrevendo "Endurecimento concluido". Sem essa linha no
  log, ele parou no meio - e a parte que costuma faltar e justamente a que
  libera a pasta de dados para o caixa gravar. O aviso diz o que fazer e onde
  esta o log, em vez de deixar o lojista descobrir ao abrir o PDV.
  --------------------------------------------------------------------------- }

procedure CheckHardening();
var
  LogPath: String;
  Content: AnsiString;
begin
  LogPath := ExpandConstant('{#DataDir}\logs\harden.log');
  if LoadStringFromFile(LogPath, Content) and
     (Pos('Endurecimento concluido', Content) > 0) then
    Exit;

  Log('harden.ps1 nao concluiu; ver ' + LogPath);
  if not WizardSilent then
    MsgBox('As permissões de segurança não foram aplicadas por completo.' + #13#10 + #13#10 +
      'Sem elas o PDV pode não conseguir gravar as vendas. Rode este instalador ' +
      'de novo como administrador; se o aviso voltar, envie ao suporte o arquivo:' + #13#10 +
      LogPath, mbCriticalError, MB_OK);
end;

{ ---------------------------------------------------------------------------
  Instalacao existente: deteccao, atualizacao e reparo.

  A versao instalada vem de dois lugares, e os dois sao lidos: o registro do
  desinstalador (o que o Inno gravou) e o proprio PDV.exe (o que de fato esta
  no disco). Se divergem, a instalacao anterior ficou pela metade - atualizacao
  interrompida, arquivo trocado a mao - e o unico reparo honesto e reinstalar
  os arquivos. Sem registro mas com PDV.exe na pasta padrao, a instalacao e
  reconhecida pelo executavel.

  Ate a 1.1.4 esta deteccao nunca funcionou: o [Code] montava a chave do
  registro com o AppId, e o "{{" do AppId e escape so do [Setup]. A chave
  procurada tinha duas chaves, nao existia, e toda instalacao por cima era
  tratada como nova - sem bloqueio de downgrade e sem aviso de fechar o caixa.

  O que o lojista escolhe:
    * versao mais nova neste instalador -> atualizar (e refazer permissoes);
    * mesma versao -> reparar tudo, ou so as permissoes e o banco (rapido);
    * versao mais velha neste instalador -> bloqueado. Um binario antigo sobre
      um banco ja migrado abriria um schema que nao conhece. Para voltar de
      versao: desinstalar (os dados ficam) e instalar a desejada.

  Nos tres casos o harden.ps1 roda de novo: e ele que devolve ao caixa a
  escrita na pasta de dados, a falha que motivou tudo isto.

  Instalacao silenciosa: atualiza ou repara tudo. Para so as permissoes:
      PDV-Setup-1.1.6.exe /SILENT /REPARO=permissoes
  --------------------------------------------------------------------------- }

var
  ExistingVersion: String;     { a versao que vale: registro, ou o exe }
  RegistryVersion: String;     { DisplayVersion do desinstalador }
  ExeVersion: String;          { versao gravada no PDV.exe instalado }
  ExistingDir: String;
  VersionComparison: Integer;  { instalada comparada a esta: <0, 0, >0 }
  Inconsistent: Boolean;
  MaintenancePage: TInputOptionWizardPage;
  QuickRepairIndex: Integer;

function UninstallKey(): String;
begin
  Result := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#AppGuid}' + '}_is1';
end;

function UninstallValue(Name: String): String;
var
  Value: String;
begin
  Result := '';
  { As duas visoes do registro: em InitializeSetup o modo 64 bits ainda nao
    esta decidido, e ler so uma poderia concluir "instalacao nova". }
  if RegQueryStringValue(HKLM64, UninstallKey(), Name, Value) then
    Result := Value
  else if RegQueryStringValue(HKLM32, UninstallKey(), Name, Value) then
    Result := Value;
end;

{ -2 quando uma das versoes nao e legivel; senao o sinal de A comparada a B. }
function CompareVersions(A, B: String): Integer;
var
  PackedA, PackedB: Int64;
begin
  if (not StrToVersion(A, PackedA)) or (not StrToVersion(B, PackedB)) then
    Result := -2
  else
  begin
    Result := ComparePackedVersion(PackedA, PackedB);
    if Result > 0 then Result := 1;
    if Result < 0 then Result := -1;
  end;
end;

procedure DetectExistingInstallation();
var
  Exe: String;
begin
  RegistryVersion := UninstallValue('DisplayVersion');
  ExistingDir := RemoveBackslashUnlessRoot(UninstallValue('InstallLocation'));
  if ExistingDir = '' then
    ExistingDir := ExpandConstant('{commonpf64}\ERPFood\PDV');

  ExeVersion := '';
  Exe := AddBackslash(ExistingDir) + '{#AppExeName}';
  if FileExists(Exe) then
    if not GetVersionNumbersString(Exe, ExeVersion) then
      ExeVersion := '';

  ExistingVersion := RegistryVersion;
  if ExistingVersion = '' then
    ExistingVersion := ExeVersion;

  Inconsistent := False;
  if RegistryVersion <> '' then
    Inconsistent := (ExeVersion = '') or (CompareVersions(RegistryVersion, ExeVersion) <> 0);

  VersionComparison := -1;
  if ExistingVersion <> '' then
    VersionComparison := CompareVersions(ExistingVersion, '{#AppVersion}');

  Log('Instalacao existente: registro="' + RegistryVersion + '" exe="' + ExeVersion +
      '" pasta="' + ExistingDir + '"');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  DetectExistingInstallation();

  if ExistingVersion = '' then
    Exit;  { instalacao nova }

  if VersionComparison = 1 then
  begin
    MsgBox('Versão mais recente já instalada: ' + ExistingVersion + '.' + #13#10 + #13#10 +
           'Instalar a {#AppVersion} por cima seria um downgrade e pode deixar ' +
           'o banco da loja num formato que esta versão não entende.' + #13#10 + #13#10 +
           'Para voltar de versão, desinstale primeiro (os dados são mantidos).',
           mbError, MB_OK);
    Result := False;
  end;
end;

function DatabaseSummary(): String;
var
  Size: Integer;
  Path: String;
begin
  Path := ExpandConstant('{#DataDir}\pdv_local.db');
  if FileSize(Path, Size) then
    Result := 'Dados da loja: ' + Path + ' (' + IntToStr(Size div 1024) + ' KB).' + #13#10 +
      'Vendas, fila de sincronização e ativação são sempre preservadas.'
  else
    Result := 'Dados da loja: nenhum banco em ' + ExpandConstant('{#DataDir}') + '.';
end;

function ExeVersionText(): String;
begin
  if ExeVersion = '' then
    Result := 'PDV.exe não encontrado em ' + ExistingDir
  else
    Result := ExeVersion;
end;

procedure InitializeWizard();
var
  Heading, Details: String;
begin
  QuickRepairIndex := -1;
  if ExistingVersion = '' then
    Exit;

  if VersionComparison = 0 then
    Heading := 'A versão {#AppVersion} já está instalada. O que fazer?'
  else
    Heading := 'Há uma versão anterior instalada. Ela será atualizada.';

  Details :=
    'Versão no registro do Windows: ' + RegistryVersion + #13#10 +
    'Versão do PDV.exe: ' + ExeVersionText() + #13#10 +
    'Pasta do programa: ' + ExistingDir + #13#10 +
    'Este instalador: {#AppVersion}' + #13#10 + #13#10 +
    DatabaseSummary() + #13#10 + #13#10;

  if Inconsistent then
    Details := Details +
      'ATENÇÃO: as duas versões não batem - a instalação anterior ficou pela ' +
      'metade. Os arquivos do programa serão reinstalados.' + #13#10 + #13#10;

  Details := Details +
    'Feche o caixa antes de continuar: o PDV aberto será encerrado e uma ' +
    'venda em andamento seria perdida.';

  MaintenancePage := CreateInputOptionPage(wpWelcome,
    'Instalação existente encontrada', Heading, Details, True, False);

  if VersionComparison = 0 then
  begin
    MaintenancePage.Add('Reparar tudo: reinstalar os arquivos do programa e refazer as permissões');
    if not Inconsistent then
      QuickRepairIndex := MaintenancePage.Add(
        'Reparar só as permissões e conferir o banco (rápido, não troca arquivos)');
  end
  else if VersionComparison = -1 then
    MaintenancePage.Add('Atualizar de ' + ExistingVersion + ' para {#AppVersion} e refazer as permissões')
  else
    MaintenancePage.Add('Reinstalar a versão {#AppVersion} e refazer as permissões');

  MaintenancePage.SelectedValueIndex := 0;
  if (QuickRepairIndex >= 0) and
     (CompareText(ExpandConstant('{param:REPARO|}'), 'permissoes') = 0) then
    MaintenancePage.SelectedValueIndex := QuickRepairIndex;
end;

{ Usado no [Files]: no reparo rapido o programa nao e recopiado. O harden.ps1,
  sim - e a versao nova dele que corrige as permissoes. }
function QuickRepair(): Boolean;
begin
  Result := (MaintenancePage <> nil) and (QuickRepairIndex >= 0) and
            (MaintenancePage.SelectedValueIndex = QuickRepairIndex);
end;

{ ---------------------------------------------------------------------------
  Codigo de ativacao para implantacao em massa.

      PDV-Setup-1.1.6.exe /SILENT /ACTIVATIONCODE=A1B2C3D4

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

{ ---------------------------------------------------------------------------
  Endereco do painel da retaguarda, para a ativacao.

      PDV-Setup-1.1.6.exe /SILENT /SERVER=painel.minhaloja.com.br /ACTIVATIONCODE=A1B2C3D4

  Sem ele o terminal nao tem para onde ativar: o endereco que vinha no codigo
  era de exemplo. Na instalacao interativa o proprio PDVSetup.exe pergunta.
  --------------------------------------------------------------------------- }

function ServerArg(Param: String): String;
var
  Server: String;
begin
  Server := ExpandConstant('{param:SERVER|}');
  if Server = '' then
    Result := ''
  else
    Result := ' --server "' + Server + '"';
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
