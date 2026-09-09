; Inno Setup script for PaperLib
; Compile with:  ISCC installer\PaperLib.iss
; Produces:      release\PaperLibSetup.exe
;
; Installs the PyInstaller onedir output (dist\PaperLib\*) into
; {autopf}\PaperLib (Program Files) with Start Menu + Desktop shortcuts.
; User data lives in %LOCALAPPDATA%\PaperLib (created by the app at runtime),
; so the installer does not create or touch any data directory.

#define MyAppName "PaperLib"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "PaperLib"
#define MyAppExeName "PaperLib.exe"

[Setup]
AppId={{7B4C2E1A-9D3F-4A6B-8C21-PAPERLIB0001}}
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
