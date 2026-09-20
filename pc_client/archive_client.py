"""ArcHive PC クライアント — 別のPCから自宅の書庫をブラウザで開くための小さなランチャー。

やることは Android アプリ（comicserver_app）の接続まわりと同じ。

  * LAN内のサーバーを探して「接続を申請」し、サーバー側で承認されると端末別トークンをもらう
  * 保存した接続先へ LAN / IPv6 / グローバルIPv4 を同時に試し、通った経路で繋ぐ
  * 「ブラウザで開く」でサーバー内蔵のビューワーを開く（トークンは中継が内部で付ける）

実行:
    pythonw archive_client.py          通常起動
    python  archive_client.py --open   繋がり次第ブラウザを開く（ショートカット向け）
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 直接実行でも隣のモジュールを読む

import tkinter as tk
from tkinter import messagebox, ttk

import client_core as core
from local_relay import LocalRelay

BG       = "#1e1e2e"
PANEL    = "#181825"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
FG_GREEN = "#a6e3a1"
FG_RED   = "#f38ba8"
FG_WARN  = "#f9e2af"
ACCENT   = "#89b4fa"

APPROVAL_TIMEOUT = 180      # 承認待ちの上限（秒）。アプリ側と同じ


def _icon_path() -> Path | None:
    here = Path(__file__).resolve().parent
    for candidate in (here / "app_icon.ico", here.parent / "assets" / "icon" / "app_icon.ico"):
        if candidate.exists():
            return candidate
    return None


class App(tk.Tk):
    def __init__(self, open_on_start: bool = False):
        super().__init__()
        self.title("ArcHive クライアント")
        self.geometry("620x560")
        self.minsize(560, 520)
        self.configure(bg=BG)
        icon = _icon_path()
        if icon:
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass

        self._cfg      = core.load_config()
        self._relay    = LocalRelay(int(self._cfg.get("relay_port") or 8766))
        self._base_url = ""          # いま実際に通っている接続先
        self._server_urls: list[str] = []   # コンボボックスの並びと同じ順のURL
        self._books    = 0
        self._busy     = False
        self._open_on_start = open_on_start

        self._build()
        self._reload_servers()
        self._log(f"設定: {core.config_path()}")
        self._log(f"端末名: {self._cfg['device_name']}（ID {self._cfg['device_id'][:8]}）")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._current_server() and (self._cfg.get("auto_connect") or open_on_start):
            self.after(300, lambda: self._connect_async(open_browser=open_on_start))
        elif open_on_start:
            self._log("[注意] 接続先が未登録です。先に「LAN内のサーバーを探す」で登録してください。")

    # ── 画面 ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=FG, arrowcolor=FG_DIM, bordercolor=PANEL,
                        selectbackground=PANEL, selectforeground=FG, padding=4)
        # readonly の既定は「選択中のテキスト」に見えて読みにくいので、状態別にも同じ色を当てる
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
                  background=[("readonly", PANEL)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", BG)

        root = tk.Frame(self, bg=BG)
        root.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)

        tk.Label(root, text="ArcHive クライアント", bg=BG, fg=ACCENT,
                 font=("Meiryo UI", 15, "bold")).pack(anchor="w")
        tk.Label(root, text="自宅のサーバーに繋いで、ブラウザで書庫を開きます。",
                 bg=BG, fg=FG_DIM, font=("Meiryo UI", 9)).pack(anchor="w", pady=(2, 12))

        # 接続先
        picker = tk.Frame(root, bg=BG)
        picker.pack(fill=tk.X)
        tk.Label(picker, text="接続先", bg=BG, fg=FG_DIM,
                 font=("Meiryo UI", 9)).pack(side=tk.LEFT, padx=(0, 8))
        self._server_var = tk.StringVar()
        self._server_box = ttk.Combobox(picker, textvariable=self._server_var,
                                        state="readonly", style="Dark.TCombobox")
        self._server_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._server_box.bind("<<ComboboxSelected>>", self._on_server_selected)

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(fill=tk.X, pady=(8, 12))
        self._find_btn = self._button(buttons, "LAN内のサーバーを探す", self._discover_async,
                                      accent=False)
        self._find_btn.pack(side=tk.LEFT)
        self._del_btn = self._button(buttons, "削除", self._remove_server, accent=False, width=8)
        self._del_btn.pack(side=tk.LEFT, padx=(8, 0))

        # 状態カード
        card = tk.Frame(root, bg=PANEL, padx=14, pady=12)
        card.pack(fill=tk.X)
        line = tk.Frame(card, bg=PANEL)
        line.pack(fill=tk.X)
        self._dot = tk.Label(line, text="●", bg=PANEL, fg=FG_RED, font=("Meiryo UI", 12))
        self._dot.pack(side=tk.LEFT, padx=(0, 6))
        self._state_lbl = tk.Label(line, text="未接続", bg=PANEL, fg=FG_RED,
                                   font=("Meiryo UI", 11, "bold"))
        self._state_lbl.pack(side=tk.LEFT)
        self._detail_lbl = tk.Label(card, text="接続先を選んで「接続」を押してください。",
                                    bg=PANEL, fg=FG_DIM, font=("Meiryo UI", 9),
                                    anchor="w", justify=tk.LEFT)
        self._detail_lbl.pack(fill=tk.X, pady=(6, 0))

        # ブラウザで開く
        self._open_btn = tk.Button(root, text="🌐 ブラウザで開く", command=self._open_browser,
                                   bg=ACCENT, fg=BG, activebackground="#b4befe",
                                   activeforeground=BG, relief=tk.FLAT,
                                   font=("Meiryo UI", 12, "bold"), cursor="hand2",
                                   height=2)
        self._open_btn.pack(fill=tk.X, pady=(14, 10))

        ops = tk.Frame(root, bg=BG)
        ops.pack(fill=tk.X)
        self._conn_btn = self._button(ops, "接続 / 再接続", lambda: self._connect_async(),
                                      accent=False)
        self._conn_btn.pack(side=tk.LEFT)
        self._scan_btn = self._button(ops, "書庫を再スキャン", self._scan_async, accent=False)
        self._scan_btn.pack(side=tk.LEFT, padx=8)
        self._token_btn = self._button(ops, "トークンをコピー", self._copy_token, accent=False)
        self._token_btn.pack(side=tk.LEFT)

        opts = tk.Frame(root, bg=BG)
        opts.pack(fill=tk.X, pady=(12, 8))
        self._auto_var   = tk.BooleanVar(value=bool(self._cfg.get("auto_connect", True)))
        self._direct_var = tk.BooleanVar(value=bool(self._cfg.get("direct_open", False)))
        self._check(opts, "起動時に自動で接続する", self._auto_var,
                    lambda: self._save_flag("auto_connect", self._auto_var.get())).pack(anchor="w")
        self._check(opts, "ローカル中継を使わず https:// を直接開く（証明書の警告が出ます）",
                    self._direct_var,
                    lambda: self._save_flag("direct_open", self._direct_var.get())).pack(anchor="w")

        tk.Label(root, text="ログ", bg=BG, fg=FG_DIM,
                 font=("Meiryo UI", 9)).pack(anchor="w", pady=(4, 2))
        self._log_box = tk.Text(root, height=8, bg=PANEL, fg=FG_DIM, relief=tk.FLAT,
                                font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED)
        self._log_box.pack(fill=tk.BOTH, expand=True)

    def _button(self, parent, text, command, accent=True, width=None) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=ACCENT if accent else PANEL, fg=BG if accent else FG,
                         activebackground="#b4befe" if accent else "#313244",
                         activeforeground=BG if accent else FG,
                         relief=tk.FLAT, cursor="hand2", width=width,
                         font=("Meiryo UI", 9), padx=10, pady=5)

    def _check(self, parent, text, var, command) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=var, command=command,
                              bg=BG, fg=FG_DIM, selectcolor=PANEL, activebackground=BG,
                              activeforeground=FG, font=("Meiryo UI", 9),
                              anchor="w", highlightthickness=0, bd=0)

    # ── 小物 ────────────────────────────────────────────────────────────────
    def _log(self, message: str) -> None:
        def write():
            self._log_box.configure(state=tk.NORMAL)
            self._log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self._log_box.see(tk.END)
            self._log_box.configure(state=tk.DISABLED)
        self.after(0, write)

    def _set_state(self, text: str, color: str, detail: str = "") -> None:
        def write():
            self._state_lbl.configure(text=text, fg=color)
            self._dot.configure(fg=color)
            if detail:
                self._detail_lbl.configure(text=detail)
        self.after(0, write)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in (self._find_btn, self._conn_btn, self._scan_btn, self._del_btn):
            self.after(0, lambda w=widget, s=state: w.configure(state=s))

    def _save(self) -> None:
        core.save_config(self._cfg)

    def _save_flag(self, key: str, value) -> None:
        self._cfg[key] = value
        self._save()

    def _current_server(self) -> dict | None:
        return core.find_server(self._cfg, self._cfg.get("selected", ""))

    def _reload_servers(self) -> None:
        servers = self._cfg.get("servers", [])
        labels  = [f"{s.get('name') or 'ArcHive'} — {s.get('url', '')}" for s in servers]
        # 接続のたびに servers の並びは変わりうる（最後に使ったものが先頭に来る）ので、
        # 表示と同時にURLも控えておき、選択はこちらで引く（番号で引くと入れ替わりでずれる）。
        self._server_urls = [s.get("url", "") for s in servers]
        self._server_box["values"] = labels
        selected = self._cfg.get("selected", "")
        for i, s in enumerate(servers):
            if s.get("url") == selected:
                self._server_box.current(i)
                break
        else:
            if servers:
                self._cfg["selected"] = servers[0]["url"]
                self._server_box.current(0)
            else:
                self._server_var.set("")
                self._set_state("接続先が未登録", FG_WARN,
                                "「LAN内のサーバーを探す」からサーバーを登録してください。")

    def _on_server_selected(self, _event=None) -> None:
        index = self._server_box.current()
        if 0 <= index < len(self._server_urls):
            self._cfg["selected"] = self._server_urls[index]
            self._save()
            self._base_url = ""
            self._set_state("未接続", FG_RED, "「接続 / 再接続」を押してください。")

    # ── 接続 ────────────────────────────────────────────────────────────────
    def _connect_async(self, open_browser: bool = False) -> None:
        server = self._current_server()
        if not server:
            self._log("接続先が未登録です。")
            return
        if self._busy:
            return
        self._set_busy(True)
        self._set_state("接続中…", FG_WARN, "経路を探しています（LAN / IPv6 / IPv4）。")
        threading.Thread(target=self._connect, args=(server, open_browser), daemon=True).start()

    def _connect(self, server: dict, open_browser: bool) -> None:
        try:
            candidates = core.build_candidates(
                server.get("url", ""), server.get("ipv6", ""),
                server.get("ipv4_global", ""), int(server.get("ipv4_port") or 0))
            token = server.get("token", "")
            fp    = server.get("cert_fingerprint", "")
            self._log(f"候補 {len(candidates)} 件を同時に試します: " + " / ".join(candidates))
            working = core.resolve_base_url(candidates, token, fp)
            if not working:
                self._base_url = ""
                # 候補レースは中で例外を握り潰すので「届かない」と「証明書が違う」が
                # 同じ結果になる。後者は対処が全く違う（再ペアリング）ので見分けて出す。
                seen = core.probe_fingerprint(server.get("url", ""))
                if seen and fp and seen != fp:
                    self._set_state("証明書が一致しません", FG_RED,
                                    "サーバーを入れ直した可能性があります。\n"
                                    "接続先を削除して、もう一度ペアリングしてください。")
                    self._log(f"[警告] 証明書が変わっています（登録 {fp[:16]}… / 現在 {seen[:16]}…）")
                else:
                    self._set_state("接続できません", FG_RED,
                                    "サーバーが起動しているか、同じLANにいるか確認してください。")
                    self._log("どの経路も応答しませんでした。")
                return

            status = core.get_status(working, token, fp) or {}
            self._base_url = working
            self._books    = int(status.get("books") or 0)
            label = core.connection_label(working)
            self._set_state(f"接続済み（{label}）", FG_GREEN,
                            f"{server.get('name') or 'ArcHive'} — {working}\n"
                            f"書庫 {self._books} 冊")
            self._log(f"接続しました: {working}（{label}）")

            # 中継を張り替える（ブラウザは同じ 127.0.0.1 のまま経路だけ切り替わる）
            if not self._cfg.get("direct_open"):
                self._ensure_relay()
                self._relay.set_target(working, fp, token)

            # サーバーのIPv6は数時間で変わるので、繋がったついでに最新を取り直す
            info = core.get_connection_info(working, token, fp)
            if info:
                updated = {"url": server.get("url", "")}
                for key, src in (("ipv6", "ipv6"), ("ipv4_global", "ipv4_global"),
                                 ("room_id", "room_id"), ("cert_fingerprint", "cert_fingerprint")):
                    value = str(info.get(src) or "")
                    if value and value != server.get(key):
                        updated[key] = value
                port = int(info.get("ipv4_port") or 0)
                if port and port != int(server.get("ipv4_port") or 0):
                    updated["ipv4_port"] = port
                if len(updated) > 1:
                    core.upsert_server(self._cfg, updated)
                    self._save()
                    self._log("接続情報（IPv6など）を更新しました。")

            if open_browser:
                self.after(0, self._open_browser)
        except core.CertificateMismatch as e:
            self._set_state("証明書が一致しません", FG_RED, str(e))
            self._log(f"[警告] {e}")
        except Exception as e:                      # 予期せぬ失敗でGUIを死なせない
            self._set_state("接続できません", FG_RED, str(e))
            self._log(f"[エラー] {e}")
        finally:
            self._set_busy(False)

    def _ensure_relay(self) -> None:
        if self._relay.running:
            return
        preferred = int(self._cfg.get("relay_port") or 8766)
        port = self._relay.start(preferred)
        if port != preferred:
            # 別のインスタンスが掴んでいる等。設定は書き換えない＝次回もまず希望のポートを試す。
            # ブラウザの読書履歴は localStorage＝オリジン（ポート）に紐づくので、
            # 一時的な衝突でポートを引っ越すと履歴が置き去りになる。
            self._log(f"[注意] ポート {preferred} が使用中のため {port} で待機します。")
        self._log(f"ローカル中継を開始しました: http://127.0.0.1:{port}")

    # ── 操作 ────────────────────────────────────────────────────────────────
    def _open_browser(self) -> None:
        server = self._current_server()
        if not server:
            messagebox.showinfo("ブラウザで開く", "先に接続先を登録してください。")
            return
        if not self._base_url:
            self._log("まだ接続していないので、先に接続します。")
            self._connect_async(open_browser=True)
            return
        if self._cfg.get("direct_open"):
            token = urllib.parse.quote(server.get("token", ""), safe="")
            webbrowser.open(f"{self._base_url}/?token={token}")
            self._log("ブラウザで開きました（直接・証明書の警告が出ます）。")
            return
        self._ensure_relay()
        self._relay.set_target(self._base_url, server.get("cert_fingerprint", ""),
                               server.get("token", ""))
        webbrowser.open(self._relay.browser_url())
        self._log("ブラウザで開きました（ローカル中継経由）。")

    def _scan_async(self) -> None:
        server = self._current_server()
        if not server or not self._base_url:
            self._log("先に接続してください。")
            return
        self._set_busy(True)
        self._log("再スキャンを依頼しました（冊数が多いと少し待ちます）…")

        def run():
            try:
                result = core.request_scan(self._base_url, server.get("token", ""),
                                           server.get("cert_fingerprint", ""))
                if result is None:
                    self._log("再スキャンに失敗しました。")
                    return
                self._books = int(result.get("books") or 0)
                self._log(f"再スキャン完了: {self._books} 冊")
                self._set_state(f"接続済み（{core.connection_label(self._base_url)}）", FG_GREEN,
                                f"{server.get('name') or 'ArcHive'} — {self._base_url}\n"
                                f"書庫 {self._books} 冊")
            finally:
                self._set_busy(False)

        threading.Thread(target=run, daemon=True).start()

    def _copy_token(self) -> None:
        server = self._current_server()
        if not server or not server.get("token"):
            self._log("コピーできるトークンがありません。")
            return
        self.clipboard_clear()
        self.clipboard_append(server["token"])
        self._log("トークンをクリップボードにコピーしました（他のブラウザや端末の手動設定用）。")

    def _remove_server(self) -> None:
        server = self._current_server()
        if not server:
            return
        name = server.get("name") or server.get("url")
        if not messagebox.askyesno("接続先の削除",
                                   f"「{name}」をこのPCから削除しますか？\n"
                                   "サーバー側の端末登録は残るので、\n"
                                   "不要ならサーバーのGUIで「取り消し」も行ってください。"):
            return
        core.remove_server(self._cfg, server.get("url", ""))
        self._save()
        self._base_url = ""
        self._reload_servers()
        self._set_state("未接続", FG_RED, "接続先を選んで「接続」を押してください。")
        self._log(f"接続先を削除しました: {name}")

    # ── LAN発見・ペアリング ─────────────────────────────────────────────────
    def _discover_async(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._log("LAN内を探しています…")

        def run():
            try:
                servers = core.discover_servers()
                self.after(0, lambda: self._show_discovered(servers))
            finally:
                self._set_busy(False)

        threading.Thread(target=run, daemon=True).start()

    def _show_discovered(self, servers: list) -> None:
        if not servers:
            self._log("サーバーが見つかりませんでした。")
            messagebox.showinfo("LAN内のサーバー",
                                "サーバーが見つかりませんでした。\n\n"
                                "・サーバーが起動しているか\n"
                                "・同じネットワーク（Wi-Fi/有線）にいるか\n"
                                "・PCのファイアウォールがUDP 8770を塞いでいないか\n"
                                "を確認してください。")
            return
        self._log(f"{len(servers)} 台見つかりました。")
        PairingDialog(self, servers)

    def _pair_finished(self, server_entry: dict) -> None:
        core.upsert_server(self._cfg, server_entry)
        self._save()
        self._reload_servers()
        self._log(f"ペアリング完了: {server_entry.get('name')} — {server_entry.get('url')}")
        self._connect_async()

    def _on_close(self) -> None:
        self._relay.stop()
        self.destroy()


class PairingDialog(tk.Toplevel):
    """見つかったサーバーを選び、承認されるまで待つ小窓。"""

    def __init__(self, app: App, servers: list):
        super().__init__(app)
        self._app     = app
        self._servers = servers
        self._timer   = None
        self._remain  = APPROVAL_TIMEOUT
        self._cancelled = False

        self.title("LAN内のサーバー")
        self.configure(bg=BG)
        self.geometry("460x360")
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._body = tk.Frame(self, bg=BG, padx=18, pady=16)
        self._body.pack(fill=tk.BOTH, expand=True)
        self._build_list()

    def _ui(self, fn) -> None:
        """閉じた後のウィンドウに after を積むと TclError になるので黙って捨てる。

        承認待ちのポーリングは別スレッドで回っており、利用者がキャンセルした直後に
        サーバー応答が返ってくることがある。
        """
        try:
            self.after(0, fn)
        except tk.TclError:
            pass

    def _clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()

    def _build_list(self) -> None:
        self._clear()
        tk.Label(self._body, text="接続するサーバーを選んでください",
                 bg=BG, fg=FG, font=("Meiryo UI", 11, "bold")).pack(anchor="w")
        tk.Label(self._body, text="選ぶとサーバーへ「接続の申請」を送ります。",
                 bg=BG, fg=FG_DIM, font=("Meiryo UI", 9)).pack(anchor="w", pady=(2, 10))
        box = tk.Listbox(self._body, bg=PANEL, fg=FG, relief=tk.FLAT,
                         selectbackground=ACCENT, selectforeground=BG,
                         font=("Meiryo UI", 10), highlightthickness=0, activestyle="none")
        box.pack(fill=tk.BOTH, expand=True)
        for s in self._servers:
            extra = []
            if s.ipv6:
                extra.append("IPv6あり")
            if s.ipv4_global:
                extra.append("IPv4直結可")
            suffix = ("   ・" + " ・".join(extra)) if extra else ""
            box.insert(tk.END, f"{s.name}   {s.host}:{s.port}{suffix}")
        box.selection_set(0)

        row = tk.Frame(self._body, bg=BG)
        row.pack(fill=tk.X, pady=(12, 0))
        tk.Button(row, text="このサーバーに接続を申請", command=lambda: self._start(box),
                  bg=ACCENT, fg=BG, relief=tk.FLAT, cursor="hand2",
                  font=("Meiryo UI", 10, "bold"), padx=12, pady=6).pack(side=tk.LEFT)
        tk.Button(row, text="キャンセル", command=self._cancel, bg=PANEL, fg=FG,
                  relief=tk.FLAT, cursor="hand2", font=("Meiryo UI", 9),
                  padx=12, pady=6).pack(side=tk.RIGHT)

    def _start(self, box: tk.Listbox) -> None:
        selection = box.curselection()
        if not selection:
            return
        server = self._servers[selection[0]]
        self._build_waiting(server)
        threading.Thread(target=self._apply_registration, args=(server,), daemon=True).start()

    def _build_waiting(self, server) -> None:
        self._clear()
        tk.Label(self._body, text="承認待ち", bg=BG, fg=FG,
                 font=("Meiryo UI", 13, "bold")).pack(pady=(20, 8))
        tk.Label(self._body,
                 text=f"{server.name} に接続を申請しました。\n\n"
                      "サーバーPCの ArcHive サーバー画面 右下「端末」欄で\n"
                      f"「{self._app._cfg['device_name']}」を承認してください。",
                 bg=BG, fg=FG_DIM, font=("Meiryo UI", 10), justify=tk.CENTER).pack()
        self._remain_lbl = tk.Label(self._body, text=f"残り約 {self._remain} 秒",
                                    bg=BG, fg=FG_DIM, font=("Meiryo UI", 9))
        self._remain_lbl.pack(pady=(18, 0))
        tk.Button(self._body, text="キャンセル", command=self._cancel, bg=PANEL, fg=FG_RED,
                  relief=tk.FLAT, cursor="hand2", font=("Meiryo UI", 9),
                  padx=12, pady=6).pack(side=tk.BOTTOM, pady=10)

    def _entry(self, server, token: str) -> dict:
        return {
            "name":             server.name,
            "url":              server.base_url,
            "token":            token,
            "ipv6":             server.ipv6,
            "ipv4_global":      server.ipv4_global,
            "ipv4_port":        server.ipv4_port,
            "room_id":          server.room_id,
            "cert_fingerprint": server.cert_fingerprint,
        }

    def _apply_registration(self, server) -> None:
        cfg = self._app._cfg
        fp  = server.cert_fingerprint
        if not fp:
            # 古いサーバーは指紋を広告しない。初回に見えた証明書を覚える（TOFU）
            fp = core.probe_fingerprint(server.base_url)
            if fp:
                server.cert_fingerprint = fp
                self._app._log(f"証明書を記憶しました（{fp[:16]}…）")
        try:
            status, data, _ = core.register_device(
                server.base_url, fp, cfg["device_id"], cfg["device_name"], server.reg_nonce)
        except Exception as exc:
            # except を抜けると exc は消えるので、後で動くlambdaに渡す前に文字列にしておく
            reason = str(exc)
            self._app._log(f"[エラー] 申請できませんでした: {reason}")
            self._ui(lambda: self._fail(f"申請できませんでした。\n{reason}"))
            return

        if status != 200:
            detail = data.get("detail") or f"HTTP {status}"
            self._app._log(f"[エラー] 申請が拒否されました: {detail}")
            self._ui(lambda: self._fail(f"申請が拒否されました。\n{detail}"))
            return

        if data.get("status") == "already_approved" and data.get("token"):
            self._app._log("この端末は承認済みでした。")
            self._ui(lambda: self._succeed(self._entry(server, data["token"])))
            return

        reg_token = data.get("reg_token", "")
        if not reg_token:
            self._ui(lambda: self._fail("サーバーの応答が想定と違いました。"))
            return
        self._app._log("接続を申請しました。サーバー側で承認してください。")
        self._poll(server, reg_token)

    def _poll(self, server, reg_token: str) -> None:
        """3秒おきに承認状態を見に行く（アプリ側 _startApprovalPolling と同じ）。"""
        deadline = time.time() + APPROVAL_TIMEOUT
        while not self._cancelled and time.time() < deadline:
            time.sleep(3)
            if self._cancelled:
                return
            self._remain = max(0, int(deadline - time.time()))
            self._ui(self._update_remain)
            try:
                status, data, _ = core.device_status(
                    server.base_url, server.cert_fingerprint, reg_token)
            except Exception:
                continue                       # 一時的な通信エラーは黙って再試行
            if status == 200 and data.get("status") == "approved":
                token = data.get("token", "")
                if token:
                    self._ui(lambda: self._succeed(self._entry(server, token)))
                    return
            elif status == 403:
                self._ui(lambda: self._fail("この端末は失効しています。\n"
                                                 "サーバー側で承認し直してください。"))
                return
            elif status == 429:
                self._ui(lambda: self._fail("サーバーが一時的に接続を拒否しています。\n"
                                                 "しばらく待ってからやり直してください。"))
                return
        if not self._cancelled:
            self._ui(lambda: self._fail("時間内に承認されませんでした。\n"
                                             "もう一度やり直してください。"))

    def _update_remain(self) -> None:
        if self.winfo_exists() and hasattr(self, "_remain_lbl"):
            try:
                self._remain_lbl.configure(text=f"残り約 {self._remain} 秒")
            except tk.TclError:
                pass

    def _succeed(self, entry: dict) -> None:
        self._cancelled = True
        self.grab_release()
        self.destroy()
        self._app._pair_finished(entry)

    def _fail(self, message: str) -> None:
        self._cancelled = True
        if self.winfo_exists():
            self.grab_release()
            self.destroy()
        messagebox.showwarning("ペアリング", message, parent=self._app)

    def _cancel(self) -> None:
        self._cancelled = True
        self.grab_release()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="ArcHive PC クライアント")
    parser.add_argument("--open", action="store_true",
                        help="接続できたら自動でブラウザを開く（ショートカット向け）")
    args = parser.parse_args()
    App(open_on_start=args.open).mainloop()


if __name__ == "__main__":
    main()
