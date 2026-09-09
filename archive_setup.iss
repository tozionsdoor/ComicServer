; ArcHive Server - Inno Setup installer script
; Prereq: build_manga_server_nuitka.bat must be run first (dist\ArcHiveServer\)
; Build:  build_archive_setup_inno.bat  ->  dist\ArcHive_Setup.exe

#define AppName "ArcHive Server"
#define AppVersion "1.1.0"
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
; 自動起動のチェックはここには置かない。アプリの「詳細設定」からスタートアップ
; フォルダのショートカットとして登録する方式に一本化した（レジストリに書かないので、
; アンインストール後に設定が残っても利用者が目で見て気づける）。

[Files]
Source: "dist\ArcHiveServer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; 自動起動はもうレジストリに登録しない。ここに残しているのは「作る」ためではなく、
; 旧バージョンのインストーラーが書いた値を消すためだけ（ValueType: none + deletevalue）。
; これが残っていると、ログオン時に旧exeが先にIPCポートを取ってしまい、
; 「新しく入れたのに古いほうが起動する」状態になる。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "ArcHiveServer"; Flags: deletevalue uninsdeletevalue

[UninstallDelete]
; アプリが実行中に作るファイルはインストーラーの記録に無いため、明示しないと
; アンインストール後もフォルダごと残ってしまう。
Type: files;          Name: "{app}\manga_server_config.json"
Type: files;          Name: "{app}\server.crt"
Type: files;          Name: "{app}\server.key"
Type: files;          Name: "{app}\.folder_lock"
; 自動起動をアプリ側でONにしていた場合のショートカット
Type: files;          Name: "{userstartup}\ArcHiveServer.lnk"
; 本文ページのキャッシュ。exeの隣ではなくユーザーフォルダ側にあり(NAS越しI/Oを避けるため)、
; 既定で最大1万ファイル・1.7GB程度まで育つので、消し忘れると一番大きなゴミになる。
; ({userprofile}という定数はInnoに無いので環境変数を使う。既定値は「存在しないパス」に
;  しておき、万一USERPROFILEが空でもドライブ直下を消しにいかないようにする)
Type: filesandordirs; Name: "{%USERPROFILE|C:\__no_such_dir__}\.manga_server"
Type: dirifempty;     Name: "{app}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "ArcHive Server を起動する"; Flags: nowait postinstall skipifsilent
