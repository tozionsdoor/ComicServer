"""
archive_setup.py - ArcHive Server セットアップ

ArcHiveServer（PCサーバー本体, PyInstaller onedir）を
ユーザーのPCにインストールする。

- インストール先（既定: %LOCALAPPDATA%\\Programs\\ArcHiveServer）にファイルをコピー
- デスクトップ / スタートメニューへショートカット作成
- Windows起動時の自動起動（任意）
- 「アプリと機能」へのアンインストーラー登録
"""
import os
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME    = "ArcHive Server"
EXE_NAME    = "ArcHiveServer.exe"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ArcHiveServer"

DEFAULT_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home())) ) / "Programs" / "ArcHiveServer"

# カラーパレット（installer.py と統一）
BG       = "#1e1e2e"
PANEL    = "#181825"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
FG_GREEN = "#a6e3a1"
FG_RED   = "#f38ba8"
ACCENT   = "#89b4fa"


def get_bundled_source() -> Path:
    """同梱されているArcHiveServer本体（onedirビルド）のパスを返す。"""
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "ArcHiveServer"
        if bundled.exists():
            return bundled
    # 開発時: build_manga_server.bat の出力をそのまま使う
    return Path(__file__).parent / "dist" / "ArcHiveServer"


UNINSTALL_BAT = r'''@echo off
setlocal
set "INSTALL_DIR=%~dp0"
if "%~1"=="/stage2" goto stage2

echo.
echo ArcHive Server をアンインストールします。
echo インストール先: %INSTALL_DIR%
echo.
choice /M "続行しますか"
if errorlevel 2 exit /b

del "%USERPROFILE%\Desktop\ArcHive Server.lnk" >nul 2>nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\ArcHive Server\ArcHive Server.lnk" >nul 2>nul
rmdir "%APPDATA%\Microsoft\Windows\Start Menu\Programs\ArcHive Server" >nul 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ArcHiveServer /f >nul 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\ArcHiveServer" /f >nul 2>nul

copy "%~f0" "%TEMP%\ArcHiveUninstall.bat" >nul
start "" cmd /c "%TEMP%\ArcHiveUninstall.bat" /stage2 "%INSTALL_DIR%"
exit /b

:stage2
set "TARGET=%~2"
:wait
timeout /t 1 /nobreak >nul
rmdir /s /q "%TARGET%" >nul 2>nul
if exist "%TARGET%" goto wait
del "%~f0" >nul 2>nul
'''


class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} セットアップ")
        self.geometry("560x420")
        self.resizable(False, False)
        self.configure(bg=BG)

        icon = get_bundled_source() / "assets" / "icon" / "app_icon.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except Exception:
                pass

        self.install_dir = tk.StringVar(value=str(DEFAULT_INSTALL_DIR))
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_startmenu = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=False)
        self.var_launch = tk.BooleanVar(value=True)

        self._build()

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build(self):
        tk.Label(self, text=f"{APP_NAME} セットアップ",
                 bg=BG, fg=FG, font=("Yu Gothic UI", 13, "bold")).pack(pady=(18, 4))
        tk.Label(self,
                 text="自炊ファイル配信サーバー本体をこのPCにインストールします。",
                 bg=BG, fg=FG_DIM, font=("Yu Gothic UI", 9)).pack()

        # インストール先
        path_f = tk.Frame(self, bg=BG)
        path_f.pack(fill="x", padx=24, pady=(20, 4))
        tk.Label(path_f, text="インストール先", bg=BG, fg=FG_DIM,
                 font=("Yu Gothic UI", 9)).pack(anchor="w")
        row = tk.Frame(path_f, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        entry = tk.Entry(row, textvariable=self.install_dir,
                          bg=PANEL, fg=FG, insertbackground=FG,
                          relief="flat", font=("Consolas", 9))
        entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        tk.Button(row, text="参照...", command=self._browse,
                  bg="#2a2a4a", fg=FG, relief="flat",
                  font=("Yu Gothic UI", 9)).pack(side="right")

        # オプション
        opt_f = tk.Frame(self, bg=PANEL)
        opt_f.pack(fill="x", padx=24, pady=(16, 4))
        for var, text in (
            (self.var_desktop,   "デスクトップにショートカットを作成する"),
            (self.var_startmenu, "スタートメニューに登録する"),
            (self.var_autostart, "Windows起動時に自動的に起動する"),
            (self.var_launch,    "インストール後にArcHive Serverを起動する"),
        ):
            tk.Checkbutton(
                opt_f, text=text, variable=var,
                bg=PANEL, fg=FG, selectcolor=PANEL,
                activebackground=PANEL, activeforeground=FG,
                font=("Yu Gothic UI", 9), anchor="w",
                highlightthickness=0, bd=0,
            ).pack(fill="x", padx=10, pady=4)

        # ステータス & 進捗
        self._status_var = tk.StringVar(value="「インストール」を押してください。")
        tk.Label(self, textvariable=self._status_var,
                 bg=BG, fg=FG_GREEN, font=("Yu Gothic UI", 9)).pack(pady=(18, 2))
        self._bar = ttk.Progressbar(self, length=480, mode="indeterminate")
        self._bar.pack(pady=4)

        # ボタン
        btn_f = tk.Frame(self, bg=BG)
        btn_f.pack(pady=10)
        self._btn_close = tk.Button(
            btn_f, text="閉じる", width=12,
            bg="#2a2a4a", fg=FG, font=("Yu Gothic UI", 10),
            relief="flat", pady=6, command=self.destroy)
        self._btn_close.pack(side="left", padx=6)
        self._btn_install = tk.Button(
            btn_f, text="インストール", width=14,
            bg="#3a3a5c", fg=FG, font=("Yu Gothic UI", 10, "bold"),
            relief="flat", pady=6, command=self._start_install)
        self._btn_install.pack(side="left", padx=6)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.install_dir.get())
        if d:
            self.install_dir.set(str(Path(d) / "ArcHiveServer"))

    # ── インストール ────────────────────────────────────────────────────────
    def _start_install(self):
        src = get_bundled_source()
        if not src.exists():
            messagebox.showerror("エラー", f"インストール元データが見つかりません。\n\n{src}")
            return
        self._btn_install.configure(state="disabled")
        self._btn_close.configure(state="disabled")
        self._bar.start(10)
        threading.Thread(target=self._install, args=(src,), daemon=True).start()

    def _install(self, src: Path):
        try:
            dest = Path(self.install_dir.get())
            self.after(0, lambda: self._status_var.set(f"コピー中... ({dest})"))
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, dirs_exist_ok=True)

            # アンインストーラーを設置
            (dest / "Uninstall.bat").write_text(UNINSTALL_BAT, encoding="utf-8")

            exe_path = dest / EXE_NAME

            if self.var_desktop.get():
                self.after(0, lambda: self._status_var.set("デスクトップショートカットを作成中..."))
                self._create_shortcut(
                    Path(os.environ["USERPROFILE"]) / "Desktop" / f"{APP_NAME}.lnk", exe_path, dest)

            if self.var_startmenu.get():
                self.after(0, lambda: self._status_var.set("スタートメニューに登録中..."))
                start_menu = (Path(os.environ["APPDATA"]) /
                               "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME)
                start_menu.mkdir(parents=True, exist_ok=True)
                self._create_shortcut(start_menu / f"{APP_NAME}.lnk", exe_path, dest)

            self.after(0, lambda: self._status_var.set("設定を登録中..."))
            self._set_autostart(exe_path, dest, self.var_autostart.get())
            self._register_uninstaller(dest, exe_path)

            self.after(0, lambda: self._done(exe_path))
        except Exception as e:
            self.after(0, lambda: self._error(str(e)))

    @staticmethod
    def _create_shortcut(link_path: Path, target: Path, workdir: Path):
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortCut(str(link_path))
            sc.TargetPath = str(target)
            sc.WorkingDirectory = str(workdir)
            sc.IconLocation = str(target)
            sc.save()
        except Exception:
            pass  # ショートカット作成失敗はインストール自体は継続させる

    @staticmethod
    def _set_autostart(exe_path: Path, workdir: Path, enable: bool):
        if winreg is None:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, "ArcHiveServer", 0, winreg.REG_SZ, f'"{exe_path}"')
                else:
                    try:
                        winreg.DeleteValue(key, "ArcHiveServer")
                    except FileNotFoundError:
                        pass
        except Exception:
            pass

    @staticmethod
    def _register_uninstaller(dest: Path, exe_path: Path):
        if winreg is None:
            return
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_UNINST_KEY) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{dest / "Uninstall.bat"}"')
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe_path))
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(dest))
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "ArcHive")
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        except Exception:
            pass

    def _done(self, exe_path: Path):
        self._bar.stop()
        self._status_var.set("インストール完了！")
        self._btn_install.configure(state="normal")
        self._btn_close.configure(state="normal")
        messagebox.showinfo("完了", f"{APP_NAME} のインストールが完了しました。\n\n{exe_path.parent}")
        if self.var_launch.get():
            try:
                os.startfile(str(exe_path))
            except Exception:
                pass
        self.destroy()

    def _error(self, msg: str):
        self._bar.stop()
        self._btn_install.configure(state="normal")
        self._btn_close.configure(state="normal")
        messagebox.showerror("エラー", f"インストールに失敗しました。\n\n{msg}")
        self._status_var.set("エラーが発生しました。再試行してください。")


if __name__ == "__main__":
    SetupApp().mainloop()
