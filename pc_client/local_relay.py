"""ブラウザ用のローカル中継（127.0.0.1 → サーバー）。

「ブラウザで開く」はサーバーの URL を直接開いてもいいが、間に中継を挟むと2つ得がある。

  * 証明書の警告が出ない
    サーバーは自己署名TLSなので https://192.168.0.25:8765 を直接開くと毎回
    「この接続ではプライバシーが保護されません」が出る（Chromeは例外を長く覚えない）。
    ブラウザ→中継は素の HTTP なので警告が出ず、中継→サーバーは
    フィンガープリント照合付きTLSなので、経路の安全性は落ちない。

  * トークンが URL に出ない
    サーバーGUIの「ブラウザで開く」は ?token=… を付けて開くため、URLバーや履歴、
    共有・スクショにトークンが残る。中継なら Authorization ヘッダを中で付けられる。

待受ポートは設定で固定する。ブラウザビューワーの読書履歴は localStorage ＝
オリジン（http://127.0.0.1:ポート）に紐づくので、毎回ポートが変わると履歴が消える。
"""
from __future__ import annotations

import http.client
import secrets
import threading
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from client_core import CertificateMismatch, PinnedHttps

# 中継が転送してはいけないヘッダ（接続ごとの取り決め＝中継の先では意味が変わる）
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}
_COOKIE_NAME = "archive_relay"


class _Handler(BaseHTTPRequestHandler):
    server_version   = "ArcHiveRelay"
    protocol_version = "HTTP/1.1"

    # 既定の実装は stderr に1行ずつ吐く（pythonw では行き先が無い）。ログはGUIへ回す。
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):     self._forward("GET")
    def do_HEAD(self):    self._forward("HEAD")
    def do_POST(self):    self._forward("POST")
    def do_PUT(self):     self._forward("PUT")
    def do_DELETE(self):  self._forward("DELETE")

    # ── 中継本体 ──
    def _forward(self, method: str) -> None:
        relay: "LocalRelay" = self.server.relay
        path, allowed, hand_out_cookie = self._check_key(relay.key)
        if not allowed:
            self._plain(403, "このURLは ArcHive クライアントから開いてください。")
            return

        client = relay.client
        if client is None or not client.base_url:
            self._plain(503, "サーバーに接続していません。ArcHive クライアントで接続してください。")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body   = self.rfile.read(length) if length else None

        head = {}
        for name, value in self.headers.items():
            low = name.lower()
            # Cookie は中継専用の鍵なので渡さない。Host/認証は中継側で付け直す。
            if low in _HOP_BY_HOP or low in ("host", "cookie", "authorization", "content-length"):
                continue
            head[name] = value
        if body is not None:
            head["Content-Length"] = str(len(body))

        try:
            res = client.request(method, path, body, head, timeout=relay.timeout)
        except CertificateMismatch as e:
            self._plain(502, f"証明書が一致しません: {e}")
            return
        except (OSError, http.client.HTTPException) as e:
            self._plain(502, f"サーバーに届きませんでした: {e}")
            return

        payload = b"" if method == "HEAD" else res.body
        self.send_response(res.status)
        for name, value in res.headers:
            low = name.lower()
            if low in _HOP_BY_HOP or low in ("content-length", "set-cookie"):
                continue        # Set-Cookie（サーバーの ms_token）は中継では不要
            self.send_header(name, value)
        if hand_out_cookie:
            self.send_header("Set-Cookie",
                             f"{_COOKIE_NAME}={relay.key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _check_key(self, key: str) -> tuple[str, bool, bool]:
        """鍵（?k= かクッキー）を確認し、サーバーへ渡すパスを返す。

        127.0.0.1 で待つ以上、同じPCのどのアプリからも叩けてしまう。ブラウザで開いている
        別サイトが勝手に中継越しに書庫を読みに来ないよう、クライアントが開いた時だけ
        入る鍵を要求する（サーバー側が ?token= → Cookie ms_token でやっているのと同じ手）。
        """
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        supplied = ""
        rest = []
        for name, value in query:
            if name == "k":
                supplied = value
            else:
                rest.append((name, value))
        # 鍵は中継の先へ渡さない（サーバーのログに出さない）
        clean = urllib.parse.urlunsplit(("", "", parts.path, urllib.parse.urlencode(rest), ""))
        if not key:
            return clean, True, False
        if supplied and secrets.compare_digest(supplied, key):
            return clean, True, True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(_COOKIE_NAME)
        if morsel and secrets.compare_digest(morsel.value, key):
            return clean, True, False
        return clean, False, False

    def _plain(self, status: int, message: str) -> None:
        payload = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False   # 他のアプリが使っているポートに相乗りしない


class LocalRelay:
    """127.0.0.1 で待ち、サーバーへ中継する。接続先はいつでも差し替えられる。"""

    def __init__(self, port: int, timeout: float = 30.0):
        self.port    = port
        self.timeout = timeout
        self.key     = secrets.token_urlsafe(16)
        self.client: PinnedHttps | None = None
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self, port: int | None = None) -> int:
        """待受を開始し、実際に使ったポートを返す。使用中なら次のポートを試す。"""
        if self._httpd:
            return self.port
        first = port or self.port
        last_error: OSError | None = None
        for candidate in range(first, first + 10):
            try:
                httpd = _Server(("127.0.0.1", candidate), _Handler)
            except OSError as e:
                last_error = e
                continue
            httpd.relay = self          # ハンドラから参照する
            self._httpd  = httpd
            self.port    = candidate
            self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            self._thread.start()
            return candidate
        raise last_error if last_error else OSError("中継ポートを確保できませんでした")

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if self.client:
            self.client.close()
            self.client = None

    def set_target(self, base_url: str, fingerprint: str, token: str) -> None:
        if self.client is None:
            self.client = PinnedHttps(base_url, fingerprint, token)
        else:
            self.client.set_target(base_url, fingerprint, token)

    def browser_url(self) -> str:
        """ブラウザに渡すURL（初回だけ鍵を付け、あとはクッキーで通す）。"""
        return f"http://127.0.0.1:{self.port}/?k={self.key}"
