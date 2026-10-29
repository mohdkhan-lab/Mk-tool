; Script generated for Mk-Tool Studio Inno Setup Installer
#define MyAppName "Mk-Tool Studio"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "MK"
#define MyAppExeName "Mk-Tool.exe"
#define MyAppIcon "app.ico"

[Setup]
AppId={{E82A1B45-7F1C-4B3A-9F02-536A18D9B420}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MkTool
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=MkTool_v{#MyAppVersion}_Setup
SetupIconFile={#MyAppIcon}
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppIcon}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Standalone Windows Launcher and Updater
Source: "Mk-Tool.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "Update-App.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

; Embedded Python Runtime & Playwright (Self-contained, no user installation needed)
Source: "..\Mk-Tool-Portable-v1.0.0\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

; App Core Engine (vidgen, bulkgen, tools with ffmpeg, refs, templates)
Source: "..\Mk-Tool-Portable-v1.0.0\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppIcon}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppIcon}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

