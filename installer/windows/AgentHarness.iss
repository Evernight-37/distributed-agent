; Per-user Agent Harness installer.
; The installer never requests elevation and never removes %LOCALAPPDATA%\AgentHarness\data.
; CI passes /DSourceDir and /DMyAppVersion. A missing version is a build error.
#ifndef SourceDir
  #define SourceDir "release\AgentHarness"
#endif
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define AppName "Agent Harness"
#define AppPublisher "Agent Harness"
#define AppId "{{B9E6D97E-8B31-4A8E-9B5D-7A6E7B4D2C10}"
#define AppExeName "AgentHarness.exe"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#MyAppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\AgentHarness\app
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir=..\..
OutputBaseFilename=AgentHarness-{#MyAppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes
UninstallDisplayName={#AppName} {#MyAppVersion}
[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
