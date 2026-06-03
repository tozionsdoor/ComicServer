"""
manga_server_setup.py - MangaServer セットアップ
C:\\keiri_python\\python_embed に必要パッケージを pip インストールする
他 PC でも同じ環境を再現できる
"""
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

PYTHON = Path(r"C:\keiri_python\python_embed\python.exe")
SP     = Path(r"C:\keiri_python\python_embed\Lib\site-packages")

# (表示名, site-packages内のフォルダ/ファイル名, pip パッケージ名, 用途)
REQUIRED_PACKAGES = [
    ("fastapi",   "fastapi",   "fastapi",        "FastAPI Webフレームワーク（サーバー本体）"),
    ("uvicorn",   "uvicorn",   "uvicorn",         "ASGI サーバー（HTTP 配信）"),
    ("starlette", "starlette", "starlette",       "Webフレームワーク基盤（FastAPI 依存）"),
    ("rarfile",   "rarfile",   "rarfile",         "RAR / CBR ファイル読み込み"),
    ("Pillow",    "PIL",       "Pillow",          "画像処理・サムネイル生成"),
    ("pymupdf",   "fitz",      "pymupdf",         "PDF 読み込み・レンダリング"),
]

# カラーパレット（installer.py と統一）
BG       = "#1e1e2e"
PANEL    = "#181825"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
FG_GREEN = "#a6e3a1"
FG_RED   = "#f38ba8"
ACCENT   = "#89b4fa"


def check_package(folder: str) -> bool:
    return (SP / folder).exists()


class SetupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MangaServer セットアップ")
        self.geometry("560x500")
        self.resizable(False, False)
        self.configure(bg=BG)

        self._build()
        self.after(200, self._check_only)

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build(self):
        # タイトル
        tk.Label(self, text="MangaServer セットアップ",
                 bg=BG, fg=FG, font=("Yu Gothic UI", 13, "bold")).pack(pady=(18, 4))
        tk.Label(self,
                 text="必要なパッケージを C:\\keiri_python\\python_embed にインストールします。",
                 bg=BG, fg=FG_DIM, font=("Yu Gothic UI", 9)).pack()

        # python.exe の存在確認
        if not PYTHON.exists():
            tk.Label(self,
                     text=f"[エラー] {PYTHON} が見つかりません。\n先に AI ツールのインストーラーを実行してください。",
                     bg=BG, fg=FG_RED, font=("Yu Gothic UI", 9),
                     justify="center").pack(pady=12)
            return

        tk.Label(self, text=f"Python: {PYTHON}",
                 bg=BG, fg=FG_DIM, font=("Consolas", 8)).pack(pady=(4, 0))

        # パッケージ一覧
        outer = tk.Frame(self, bg=PANEL)
        outer.pack(fill="both", expand=True, padx=20, pady=(12, 4))

        header = tk.Frame(outer, bg=PANEL)
        header.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(header, text="パッケージ",  bg=PANEL, fg="#7f849c",
                 font=("Yu Gothic UI", 8), width=14, anchor="w").pack(side="left")
        tk.Label(header, text="用途",        bg=PANEL, fg="#7f849c",
                 font=("Yu Gothic UI", 8), anchor="w").pack(side="left")
        tk.Label(header, text="状態",        bg=PANEL, fg="#7f849c",
                 font=("Yu Gothic UI", 8), width=12, anchor="e").pack(side="right")
        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=8)

        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        sf     = tk.Frame(canvas, bg=PANEL)
        sf.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sf, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        self._status_lbls: dict[str, tk.Label] = {}
        for pkg_name, _, _, desc in REQUIRED_PACKAGES:
            row = tk.Frame(sf, bg=PANEL)
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=pkg_name, bg=PANEL, fg=FG,
                     font=("Yu Gothic UI", 9), width=14, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg=PANEL, fg=FG_DIM,
                     font=("Yu Gothic UI", 8), anchor="w").pack(side="left")
            lbl = tk.Label(row, text="－", bg=PANEL, fg="#585b70",
                           font=("Yu Gothic UI", 9, "bold"), width=12, anchor="e")
            lbl.pack(side="right")
            self._status_lbls[pkg_name] = lbl

        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=8, pady=(4, 0))

        # ステータスとプログレス
        self._status_var = tk.StringVar(value="「インストール」を押してください。")
        tk.Label(self, textvariable=self._status_var,
                 bg=BG, fg=FG_GREEN, font=("Yu Gothic UI", 9)).pack(pady=(6, 2))
        self._bar = ttk.Progressbar(self, length=500, mode="indeterminate")
        self._bar.pack(pady=4)

        # ボタン
        btn_f = tk.Frame(self, bg=BG)
        btn_f.pack(pady=8)
        self._btn_check = tk.Button(
            btn_f, text="状態確認", width=12,
            bg="#2a2a4a", fg=FG, font=("Yu Gothic UI", 10),
            relief="flat", pady=6, command=self._check_only)
        self._btn_check.pack(side="left", padx=6)
        self._btn_install = tk.Button(
            btn_f, text="インストール", width=14,
            bg="#3a3a5c", fg=FG, font=("Yu Gothic UI", 10, "bold"),
            relief="flat", pady=6, command=self._start_install)
        self._btn_install.pack(side="left", padx=6)

    # ── 状態確認 ────────────────────────────────────────────────────────────
    def _check_only(self):
        if not SP.exists():
            for lbl in self._status_lbls.values():
                lbl.config(text="未インストール", fg=FG_RED)
            self._status_var.set("keiri_python が見つかりません。")
            return
        for pkg_name, folder, _, _ in REQUIRED_PACKAGES:
            lbl = self._status_lbls[pkg_name]
            if check_package(folder):
                lbl.config(text="✓  OK", fg=FG_GREEN)
            else:
                lbl.config(text="✗  なし", fg=FG_RED)
        missing = [p for p, f, _, _ in REQUIRED_PACKAGES if not check_package(f)]
        if missing:
            self._status_var.set(f"未インストール: {', '.join(missing)}")
        else:
            self._status_var.set("すべてインストール済みです。")

    # ── インストール ────────────────────────────────────────────────────────
    def _start_install(self):
        if not PYTHON.exists():
            messagebox.showerror("エラー", f"Python が見つかりません:\n{PYTHON}")
            return
        self._btn_install.configure(state="disabled")
        self._btn_check.configure(state="disabled")
        self._bar.start(10)
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        missing = [pip for _, folder, pip, _ in REQUIRED_PACKAGES
                   if not check_package(folder)]
        if not missing:
            self.after(0, lambda: self._status_var.set("すべてインストール済みです。"))
            self.after(0, self._done)
            return

        for pip_name in missing:
            self.after(0, lambda n=pip_name: self._status_var.set(f"インストール中: {n} ..."))
            try:
                result = subprocess.run(
                    [str(PYTHON), "-m", "pip", "install", pip_name,
                     "--quiet", "--no-warn-script-location"],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode != 0:
                    err = result.stderr or result.stdout
                    raise RuntimeError(f"{pip_name} のインストールに失敗:\n{err[:300]}")
            except subprocess.TimeoutExpired:
                self.after(0, lambda n=pip_name: self._error(
                    f"{n} のインストールがタイムアウトしました。\nネットワーク接続を確認してください。"))
                return
            except Exception as e:
                self.after(0, lambda msg=str(e): self._error(msg))
                return

        self.after(0, self._done)

    def _done(self):
        self._bar.stop()
        self._check_only()
        missing = [p for p, f, _, _ in REQUIRED_PACKAGES if not check_package(f)]
        if missing:
            self._status_var.set(f"インストール後も見つからない: {', '.join(missing)}")
            messagebox.showwarning("確認", f"一部パッケージが見つかりません:\n{', '.join(missing)}")
        else:
            self._status_var.set("インストール完了！")
            messagebox.showinfo("完了",
                "すべてのパッケージがインストールされました。\n\nmanga_server_app.py を起動できます。")
        self._btn_install.configure(state="normal")
        self._btn_check.configure(state="normal")

    def _error(self, msg: str):
        self._bar.stop()
        self._btn_install.configure(state="normal")
        self._btn_check.configure(state="normal")
        messagebox.showerror("エラー", f"インストールに失敗しました。\n\n{msg}")
        self._status_var.set("エラーが発生しました。再試行してください。")


if __name__ == "__main__":
    SetupApp().mainloop()
