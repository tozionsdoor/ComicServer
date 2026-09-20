"""ArcHive PC クライアントの中核処理（GUI非依存・ここだけで単体テストできる）。

Android アプリ（comicserver_app）と同じ手順でサーバーに繋ぐ:

  1. LAN へ UDP ブロードキャストを投げてサーバーを探す        … discovery_service.dart 相当
  2. 見つけたサーバーへ端末登録を申請し、承認されるまで待つ    … login_screen.dart 相当
  3. 承認で受け取った「端末別トークン」を保存し、以後はそれで接続する
  4. LAN / IPv6 / グローバルIPv4 の候補を同時に叩き、最初に応答した経路を使う
                                                              … api_service.dart 相当

サーバーは自己署名TLSなので、証明書はフィンガープリント（SHA-256 of DER）で検証する。
フィンガープリントは LAN 発見の応答に入っているので、ペアリング時に受け取って保存する
（Flutter 側 http_pinned_client.dart と同じ TOFU + ピンニング）。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import queue
import secrets
import select
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DISCOVERY_PORT  = 8770                      # サーバーの DISCOVERY_PORT と一致させること
DISCOVERY_PROBE = b"COMICSERVER_DISCOVER"
DEFAULT_PORT    = 8765


# ─── 設定ファイル ──────────────────────────────────────────────────────────────
def app_dir() -> Path:
    """設定の保存先。ARCHIVE_CLIENT_DIR があればそちら（テスト時の逃がし先）。"""
    override = os.environ.get("ARCHIVE_CLIENT_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "ArcHiveClient"
    return Path.home() / ".archive_client"


DEFAULT_CONFIG: dict = {
    "device_id":    "",      # 初回起動時に UUID v4 を生成（端末の身元。サーバーの承認単位）
    "device_name":  "",      # 既定はPC名。サーバーの端末一覧にこの名前で出る
    "servers":      [],      # 接続先（url/token/ipv6/ipv4_global/ipv4_port/room_id/cert_fingerprint）
    "selected":     "",      # 最後に使った url
    "relay_port":   8766,    # ローカル中継の待受ポート（読書履歴はこのオリジンに紐づくので固定したい）
    "auto_connect": True,    # 起動時に自動で繋ぐ
    "direct_open":  False,   # True ならローカル中継を使わず https://… を直接ブラウザで開く
}


def config_path() -> Path:
    return app_dir() / "client_config.json"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except (OSError, ValueError):
            pass            # 壊れていたら既定値で作り直す（接続はやり直せば済む）
    changed = False
    if not cfg.get("device_id"):
        cfg["device_id"] = str(uuid.uuid4())
        changed = True
    if not cfg.get("device_name"):
        cfg["device_name"] = default_device_name()
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    """一時ファイルに書いてから置き換える（保存中に落ちても設定を壊さない）。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def default_device_name() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "PC"
    return f"PC ({host})"[:50]      # サーバー側は device_name を50文字で切る


def find_server(cfg: dict, url: str) -> dict | None:
    for s in cfg.get("servers", []):
        if s.get("url") == url:
            return s
    return None


def upsert_server(cfg: dict, entry: dict) -> dict:
    """接続先を保存（同じURLは上書きして先頭へ）。既存の値は消さずに重ねる。"""
    url = entry.get("url", "")
    if not url:
        return cfg
    servers = [s for s in cfg.get("servers", []) if s.get("url") != url]
    merged = dict(find_server(cfg, url) or {})
    merged.update({k: v for k, v in entry.items() if v not in ("", None)})
    merged["url"] = url
    cfg["servers"] = [merged] + servers
    cfg["selected"] = url
    return cfg


def remove_server(cfg: dict, url: str) -> dict:
    cfg["servers"] = [s for s in cfg.get("servers", []) if s.get("url") != url]
    if cfg.get("selected") == url:
        cfg["selected"] = cfg["servers"][0]["url"] if cfg["servers"] else ""
    return cfg


# ─── LAN 自動発見 ─────────────────────────────────────────────────────────────
@dataclass
class DiscoveredServer:
    name:             str
    host:             str
    port:             int
    ipv6:             str = ""
    ipv4_global:      str = ""
    ipv4_port:        int = 0
    token:            str = ""   # 旧サーバー互換（新サーバーは空）
    reg_nonce:        str = ""   # 端末登録ノンス
    room_id:          str = ""
    cert_fingerprint: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"


def _default_route_ipv4() -> str:
    """既定ルートで外に出るときの送信元IP。複数NIC環境での「どのLANか」の当たり。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _local_ipv4s() -> list[str]:
    """このPCが持つIPv4を、既定ルートのものを先頭にして列挙する。

    PCは VirtualBox / Hyper-V / 未接続Wi-Fi(169.254.x) など仮想NICを抱えがちで、
    bind せずにブロードキャストすると別NICから出てサーバーに届かないことがある
    （サーバー側でも SSDP で同じ罠を踏んでいる）。NICごとに socket を作って全部から投げる。
    """
    ips: list[str] = []
    first = _default_route_ipv4()
    if first:
        ips.append(first)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["0.0.0.0"]


def _broadcast_targets(ip: str) -> list[str]:
    """そのNICから投げる宛先。制限ブロードキャストと、/24 前提のサブネット宛て。"""
    targets = ["255.255.255.255"]
    parts = ip.split(".")
    if len(parts) == 4 and ip != "0.0.0.0":
        subnet = ".".join(parts[:3] + ["255"])
        if subnet not in targets:
            targets.append(subnet)
    return targets


def _parse_discovery(data: bytes, from_ip: str) -> DiscoveredServer | None:
    try:
        m = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(m, dict) or m.get("service") != "comicserver":
        return None
    return DiscoveredServer(
        name=str(m.get("name") or "ComicServer"),
        host=str(m.get("host") or from_ip),
        port=int(m.get("port") or DEFAULT_PORT),
        ipv6=str(m.get("ipv6") or ""),
        ipv4_global=str(m.get("ipv4_global") or ""),
        ipv4_port=int(m.get("ipv4_port") or 0),
        token=str(m.get("token") or ""),
        reg_nonce=str(m.get("reg_nonce") or ""),
        room_id=str(m.get("room_id") or ""),
        cert_fingerprint=str(m.get("cert_fingerprint") or ""),
    )


def discover_servers(timeout: float = 2.0) -> list[DiscoveredServer]:
    """LAN にブロードキャストを投げ、応答したサーバー一覧を返す。失敗しても空リスト。"""
    found: dict[str, DiscoveredServer] = {}
    socks: list[socket.socket] = []
    for ip in _local_ipv4s():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            socks.append(s)
        except OSError:
            continue
    if not socks:
        return []

    deadline   = time.time() + timeout
    probes     = 0
    next_probe = 0.0
    try:
        while True:
            now = time.time()
            if probes < 3 and now >= next_probe:      # パケットロス対策に数回投げる
                for s in socks:
                    for dest in _broadcast_targets(s.getsockname()[0]):
                        try:
                            s.sendto(DISCOVERY_PROBE, (dest, DISCOVERY_PORT))
                        except OSError:
                            pass
                probes += 1
                next_probe = now + 0.2
            remain = deadline - time.time()
            if remain <= 0:
                break
            ready, _, _ = select.select(socks, [], [], min(remain, 0.2))
            for s in ready:
                try:
                    data, addr = s.recvfrom(8192)
                except OSError:
                    continue
                srv = _parse_discovery(data, addr[0])
                if srv:
                    found[f"{srv.host}:{srv.port}"] = srv
    finally:
        for s in socks:
            s.close()
    return list(found.values())


# ─── 証明書ピンニング付き HTTPS クライアント ───────────────────────────────────
class CertificateMismatch(Exception):
    """証明書が保存済みフィンガープリントと違う（サーバー入れ替え or 中間者）。"""


# NAS版サーバー（ReadyNAS = Debian 8 + 古いOpenSSL）は TLS1.2 でも
# ECDHE-RSA-AES256-SHA のような SHA-1 MAC の暗号しか喋らない。こちらのPythonが使う
# OpenSSL 3.x は既定のセキュリティレベル2でそれを弾き、ハンドシェイクが
# 「UNEXPECTED_EOF_WHILE_READING」で落ちる（＝サーバーが落ちているように見える）。
#
# 既定のまま繋ぎに行き、TLSで落ちた相手にだけレベルを1に下げて繋ぎ直す。
# 相手は証明書のフィンガープリントで固定しているので、暗号が古くても
# 「知らないサーバーに繋がる」ことは起きない。一度判明した相手は覚えておき、
# 以降の接続では二度手間にしない。
_LEGACY_CIPHERS = "DEFAULT@SECLEVEL=1"
_legacy_tls_hosts: set[tuple[str, int]] = set()
_legacy_tls_lock = threading.Lock()


def _open_tls(host: str, port: int, timeout: float,
              legacy: bool) -> http.client.HTTPSConnection:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False          # IP直打ち＋自己署名なので名前では検証できない
    ctx.verify_mode    = ssl.CERT_NONE  # 代わりにフィンガープリント照合で担保する
    if legacy:
        try:
            ctx.set_ciphers(_LEGACY_CIPHERS)
        except ssl.SSLError:
            pass                        # この環境のOpenSSLが解釈できなければ既定のまま
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    conn.connect()
    return conn


@dataclass
class Response:
    status:  int
    headers: list = field(default_factory=list)
    body:    bytes = b""

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else {}
        except (ValueError, UnicodeDecodeError):
            return {}


class PinnedHttps:
    """自己署名証明書をフィンガープリントで検証する HTTPS クライアント。

    接続は使い回すが、寝かせるのは IDLE_LIMIT 秒まで。uvicorn は keep-alive を
    5秒で切るので、それより長くプールに残すと「取り出した瞬間に死んでいる接続」を
    掴む。Flutter 側で1ページ目がブロークンアイコンになった原因がこれで、
    あちらは idleTimeout=4秒 で解決している（さらに保険として1回だけ張り直す）。
    """

    IDLE_LIMIT = 3.0

    def __init__(self, base_url: str, fingerprint: str = "", token: str = ""):
        self._lock = threading.Lock()
        self._pool: list[tuple[http.client.HTTPSConnection, float]] = []
        self.base_url    = ""
        self.fingerprint = ""
        self.token       = ""
        self.observed_fingerprint = ""     # TOFU用（初回接続で実際に見えた指紋）
        self.set_target(base_url, fingerprint, token)

    def set_target(self, base_url: str, fingerprint: str | None = None,
                   token: str | None = None) -> None:
        """接続先を差し替える（経路が LAN→IPv6 に変わった時など）。プールは捨てる。"""
        with self._lock:
            self.base_url = (base_url or "").rstrip("/")
            if fingerprint is not None:
                self.fingerprint = fingerprint.strip().lower()
            if token is not None:
                self.token = token
            pool, self._pool = self._pool, []
        _close_all(pool)

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, []
        _close_all(pool)

    # ── 接続の取得／返却 ──
    def _new_conn(self, timeout: float) -> http.client.HTTPSConnection:
        parts = urllib.parse.urlsplit(self.base_url)
        host  = parts.hostname or ""
        port  = parts.port or DEFAULT_PORT
        with _legacy_tls_lock:
            legacy = (host, port) in _legacy_tls_hosts
        try:
            conn = _open_tls(host, port, timeout, legacy)
        except (ssl.SSLError, ConnectionResetError):
            if legacy:
                raise
            conn = _open_tls(host, port, timeout, True)   # 古いサーバー向けに緩めて再挑戦
            with _legacy_tls_lock:
                _legacy_tls_hosts.add((host, port))
        der = conn.sock.getpeercert(binary_form=True) or b""
        fp  = hashlib.sha256(der).hexdigest()
        self.observed_fingerprint = fp
        if self.fingerprint and not secrets.compare_digest(fp, self.fingerprint):
            conn.close()
            raise CertificateMismatch(
                f"証明書が登録時と違います（期待 {self.fingerprint[:16]}… / 実際 {fp[:16]}…）")
        return conn

    def _take(self, timeout: float) -> http.client.HTTPSConnection:
        while True:
            with self._lock:
                if not self._pool:
                    break
                conn, idle_since = self._pool.pop()
            if time.time() - idle_since < self.IDLE_LIMIT:
                try:
                    conn.sock.settimeout(timeout)
                    return conn
                except (OSError, AttributeError):
                    pass
            _close_all([(conn, 0.0)])
        return self._new_conn(timeout)

    def _give_back(self, conn: http.client.HTTPSConnection) -> None:
        with self._lock:
            self._pool.append((conn, time.time()))

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, timeout: float = 10.0,
                retries: int = 1) -> Response:
        last: Exception | None = None
        for _ in range(retries + 1):
            conn = None
            try:
                conn = self._take(timeout)
                head = {}
                if self.token:
                    head["Authorization"] = f"Bearer {self.token}"
                if headers:
                    head.update(headers)
                conn.request(method, path, body=body, headers=head)
                res  = conn.getresponse()
                data = res.read()
                out  = Response(res.status, res.getheaders(), data)
                if res.will_close:
                    _close_all([(conn, 0.0)])
                else:
                    self._give_back(conn)
                return out
            except CertificateMismatch:
                if conn is not None:
                    _close_all([(conn, 0.0)])
                raise
            except (OSError, http.client.HTTPException) as e:
                if conn is not None:
                    _close_all([(conn, 0.0)])
                last = e                    # 寝ていた接続を掴んだだけなら張り直せば通る
        raise last if last else OSError("接続できませんでした")


def _close_all(pool) -> None:
    for conn, _ in pool:
        try:
            conn.close()
        except Exception:
            pass


def _one_shot(base_url: str, fingerprint: str, method: str, path: str,
              payload: dict | None = None, token: str = "",
              timeout: float = 10.0) -> tuple[int, dict, str]:
    """使い捨て接続で JSON をやり取りする。戻り値は (ステータス, JSON, 実際の指紋)。"""
    cli = PinnedHttps(base_url, fingerprint, token)
    try:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        head = {"Content-Type": "application/json"} if body is not None else None
        res  = cli.request(method, path, body, head, timeout=timeout)
        return res.status, res.json(), cli.observed_fingerprint
    finally:
        cli.close()


# ─── サーバーAPI ──────────────────────────────────────────────────────────────
def register_device(base_url: str, fingerprint: str, device_id: str,
                    device_name: str, reg_nonce: str,
                    timeout: float = 10.0) -> tuple[int, dict, str]:
    """端末登録を申請する。戻りは status=pending（承認待ち）か already_approved。"""
    return _one_shot(base_url, fingerprint, "POST", "/api/devices/register", {
        "device_id":   device_id,
        "device_name": device_name,
        "reg_nonce":   reg_nonce,
    }, timeout=timeout)


def device_status(base_url: str, fingerprint: str, reg_token: str,
                  timeout: float = 5.0) -> tuple[int, dict, str]:
    """承認状態をポーリングする。承認されると token が返る。"""
    path = "/api/devices/status?reg_token=" + urllib.parse.quote(reg_token, safe="")
    return _one_shot(base_url, fingerprint, "GET", path, timeout=timeout)


def get_status(base_url: str, token: str, fingerprint: str = "",
               timeout: float = 5.0) -> dict | None:
    """/api/status。繋がらない・認証が通らないなら None。"""
    try:
        status, data, _ = _one_shot(base_url, fingerprint, "GET", "/api/status",
                                    token=token, timeout=timeout)
    except (OSError, http.client.HTTPException, CertificateMismatch):
        return None
    return data if status == 200 else None


def request_scan(base_url: str, token: str, fingerprint: str = "",
                 timeout: float = 120.0) -> dict | None:
    """書庫の再スキャンを依頼する（冊数が返る）。"""
    try:
        status, data, _ = _one_shot(base_url, fingerprint, "POST", "/api/scan",
                                    token=token, timeout=timeout)
    except (OSError, http.client.HTTPException, CertificateMismatch):
        return None
    return data if status == 200 else None


def probe_fingerprint(base_url: str, timeout: float = 5.0) -> str:
    """相手の証明書の指紋を見てくるだけ（TOFU用）。繋がらなければ空文字。"""
    cli = PinnedHttps(base_url)
    try:
        cli.request("GET", "/api/status", timeout=timeout, retries=0)
    except Exception:
        pass
    finally:
        cli.close()
    return cli.observed_fingerprint


def get_connection_info(base_url: str, token: str, fingerprint: str = "",
                        timeout: float = 5.0) -> dict | None:
    """最新のIPv6/グローバルIPv4を取り直す（サーバーのIPv6は数時間で変わる）。"""
    try:
        status, data, _ = _one_shot(base_url, fingerprint, "GET", "/api/connection-info",
                                    token=token, timeout=timeout)
    except (OSError, http.client.HTTPException, CertificateMismatch):
        return None
    return data if status == 200 else None


# ─── 接続先候補 ───────────────────────────────────────────────────────────────
def build_candidates(primary_url: str, ipv6: str = "", ipv4_global: str = "",
                     ipv4_port: int = 0) -> list[str]:
    """保存済み設定から接続候補を優先順に作る（api_service.dart の buildCandidates 相当）。

    1) primary_url（LAN直・手動入力）
    2) [ipv6]:port      … IPv6が使える回線で勝つ
    3) ipv4_global:port … UPnPでポート開放済みの回線で勝つ（外部ポートは内部と違うことがある）
    """
    out: list[str] = []

    def add(url: str) -> None:
        if not url:
            return
        v = url.rstrip("/")
        if v.startswith("http://"):          # 旧設定の http:// を https:// に正規化
            v = "https://" + v[len("http://"):]
        if v not in out:
            out.append(v)

    add(primary_url)
    parts    = urllib.parse.urlsplit(primary_url if "//" in primary_url else "https://" + primary_url)
    def_port = parts.port or DEFAULT_PORT
    if ipv6:
        add(f"https://[{ipv6}]:{def_port}")
    if ipv4_global:
        add(f"https://{ipv4_global}:{ipv4_port if ipv4_port > 0 else def_port}")
    return out


def resolve_base_url(candidates: list[str], token: str, fingerprint: str = "",
                     timeout: float = 4.0) -> str | None:
    """候補へ同時に /api/status を投げ、最初に応答したものを採用する（Happy Eyeballs）。

    家ではLAN直、外出先ではIPv6やグローバルIPv4が自動的に勝つ。全滅なら None。
    """
    if not candidates:
        return None
    results: queue.Queue = queue.Queue()

    def probe(base: str) -> None:
        ok = None
        try:
            status, _, _ = _one_shot(base, fingerprint, "GET", "/api/status",
                                     token=token, timeout=timeout)
            if status == 200:
                ok = base
        except Exception:
            pass                              # この候補は不通
        results.put(ok)

    for base in candidates:
        threading.Thread(target=probe, args=(base,), daemon=True).start()
    for _ in candidates:
        try:
            got = results.get(timeout=timeout + 1.0)
        except queue.Empty:
            return None
        if got:
            return got
    return None


def connection_label(url: str) -> str:
    """今どの経路で繋がっているかの表示用ラベル（api_service.dart の connectionLabel 相当）。"""
    host = urllib.parse.urlsplit(url).hostname or url
    if ":" in host:
        return "IPv6直結"
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 127 or a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
            return "LAN直結"
    return "IPv4直結（外部）"
