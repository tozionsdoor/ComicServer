; ArcHive Server - Inno Setup installer script
; Prereq: build_manga_server_nuitka.bat must be run first (dist\ArcHiveServer\)
; Build:  build_archive_setup_inno.bat  ->  dist\ArcHive_Setup.exe

#define AppName "ArcHive Server"
#define AppVersion "1.0.0"
#define AppPublisher "ArcHive"
#define AppExeName "ArcHiveServer.exe"

[Setup]
AppId={{F3A8D2C1-B7E4-4F9A-8C3D-1E5B6A7F2D90}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL=https://github.com/tozionsdoor/ComicServer
DefaultDirName={localappdata}\Programs\ArcHiveServer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=ArcHive_Setup
SetupIconFile=assets\icon\app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
MinVersion=10.0

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "追加タスク:"
Name: "autostart"; Description: "Windows 起動時に自動起動する"; GroupDescription: "追加タスク:"

[Files]
Source: "dist\manga_server_app.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ArcHiveServer"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "ArcHive Server を起動する"; Flags: nowait postinstall skipifsilent
