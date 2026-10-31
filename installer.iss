; MK Story Studio - Inno Setup installer.
; Built by GitHub Actions (.github/workflows/release.yml): the payload\
; folder next to this script holds MKStoryStudio.exe + app\ + runtime\.
; MyAppVersion is passed on the ISCC command line from vidgen\version.py.
#ifndef MyAppVersion
#define MyAppVersion "1.2.0"
#endif
#define MyAppName "MK Story Studio"
#define MyAppPublisher "MK"
#define MyAppExeName "MKStoryStudio.exe"

[Setup]
AppId={{E82A1B45-7F1C-4B3A-9F02-536A18D9B420}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\MKStoryStudio
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=MKStoryStudio_v{#MyAppVersion}_Setup
SetupIconFile=app.ico
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\app.ico
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Native launcher + icon
Source: "payload\MKStoryStudio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "payload\app.ico"; DestDir: "{app}"; Flags: ignoreversion

; App core (Python source: vidgen, bulkgen, ai_avatar_pipeline, tools)
Source: "payload\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

; Embedded Python runtime + Playwright browsers (self-contained)
Source: "payload\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
