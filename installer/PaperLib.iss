; Inno Setup script for PaperLib
; Compile with:  ISCC installer\PaperLib.iss
; Produces:      release\PaperLibSetup.exe
;
; Installs the PyInstaller onedir output (dist\PaperLib\*) into
; {autopf}\PaperLib (Program Files) with Start Menu + Desktop shortcuts.
; User data lives in %LOCALAPPDATA%\PaperLib (created by the app at runtime),
; so the installer does not create or touch any data directory.

#define MyAppName "PaperLib"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "PaperLib"
#define MyAppExeName "PaperLib.exe"

[Setup]
; Fixed AppId GUID: keeps upgrades/uninstalls tied to the same product so a
; reinstall replaces the previous install cleanly. The leading '{{' is Inno's
; escape for a literal '{'.
AppId={{63FF2543-E11E-41E1-BA9A-59DEBA5AFD6C}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Install to Program Files -> requires elevation.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=PaperLibSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Bundle the entire onedir build produced by PyInstaller.
Source: "..\dist\PaperLib\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
{ On uninstall, always wipe the per-user data folder
  (%LOCALAPPDATA%\PaperLib), which holds the copied papers and the library.db.
  The app only ever stores COPIES of PDFs there, so removing it is safe and
  guarantees a truly clean uninstall that leaves nothing behind. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\PaperLib');
    if DirExists(DataDir) then
      DelTree(DataDir, True, True, True);
  end;
end;
