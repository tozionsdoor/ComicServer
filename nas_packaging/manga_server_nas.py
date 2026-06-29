# coding: utf-8
from __future__ import annotations
"""
manga_server_nas.py - ArcHive NAS ヘッドレスサーバー
manga_server_app.py から tkinter/pystray/WebRTC を除いた Linux NAS 向け版。
systemd サービスとして動作し、/admin で Web 管理ページを提供する。

削除: tkinter GUI / pystray トレイ / WebRTC(aiortc) P2P シグナリング
追加: /admin Web 管理ページ / Linux パス / systemd 対応 main()
保持: FastAPI サーバー / UPnP (IPv4/IPv6) / LAN 自動発見 / TLS / キャッシュ
"""

import asyncio
import base64
import datetime
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import rarfile
try:
    import pymupdf
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
from PIL import Image
import warnings
warnings.filterwarnings('error', category=Image.DecompressionBombWarning)
Image.MAX_IMAGE_PIXELS = 50_000_000  # 50MP超はOOMリスクがあるため弾く（armelはRAM 512MB）

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import Response, HTMLResponse
import uvicorn

import hashlib

# ─── ロギング設定（systemd journal / stdout に流れる） ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
_logger = logging.getLogger("archive")

class _LogQueue:
    """manga_server_app.py との互換シム。put() をそのまま logging.info() に流す。"""
    def put(self, msg: str) -> None:
        _logger.info(str(msg))

_log_queue = _LogQueue()

# ─── パス設定（環境変数で上書き可能） ────────────────────────────────────────
APP_DIR   = Path(os.environ.get("ARCHIVE_APP_DIR",   "/apps/archiveserver"))
CACHE_DIR = Path(os.environ.get("ARCHIVE_CACHE_DIR", str(APP_DIR / "cache")))

CONFIG_PATH    = APP_DIR / "manga_server_config.json"
CERT_PATH      = APP_DIR / "server.crt"
KEY_PATH       = APP_DIR / "server.key"
PAGE_CACHE_DIR = CACHE_DIR / "pages"

# ─── unrar パス（Linux） ───────────────────────────────────────────────────────
UNRAR_PATH      = ""
UNRAR_AVAILABLE = False
for _candidate in [
    "/usr/bin/unrar",
    "/usr/local/bin/unrar",
    "/usr/bin/unrar-free",
    "/usr/local/bin/unrar-free",
]:
    if Path(_candidate).exists():
        UNRAR_PATH      = _candidate
        UNRAR_AVAILABLE = True
        rarfile.UNRAR_TOOL = UNRAR_PATH
        break

# ─── 定数 ────────────────────────────────────────────────────────────────────
IMAGE_EXT    = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
FITZ_EXT     = {'.pdf', '.epub'} if PYMUPDF_AVAILABLE else set()
ARCHIVE_EXT  = {'.zip', '.rar', '.cbz', '.cbr'} | FITZ_EXT
MIN_FOLDER_IMAGES = 3
COVER_W, COVER_H  = 200, 280
PAGE_MAX          = 1800
PAGE_CACHE_MAX    = 4000

# ─── Firebase シグナリング設定（接続情報の Firebase 登録に使用） ─────────────────
DEFAULT_FIREBASE: dict = {
    "api_key":      "AIzaSyCHz05od7Ta6wJFKSWcTisWfhJh_kg1fKQ",
    "database_url": "https://comicserver-default-rtdb.asia-southeast1.firebasedatabase.app",
}

DEFAULT_CONFIG: dict = {
    "scan_dirs":         [],
    "room_id":           "",
    "port":              8765,
    "host":              "0.0.0.0",
    "upnp_ipv4_open":   True,
    "firebase":          {},
    "stun_servers":      ["stun:stun.l.google.com:19302"],
    "turn":              {"url": "", "username": "", "credential": ""},
}

# ─── グローバル状態 ────────────────────────────────────────────────────────────
_books:     dict[str, dict] = {}
_preloading: bool           = False
_config:    dict            = {}
_cert_fingerprint: str      = ""

BG_QUIET_SECONDS    = 2.0
_last_user_activity = 0.0

def _note_user_activity() -> None:
    global _last_user_activity
    _last_user_activity = time.monotonic()

def _bg_wait_while_active() -> None:
    while (time.monotonic() - _last_user_activity) < BG_QUIET_SECONDS:
        time.sleep(0.2)

_reg_nonces:    dict[str, float]             = {}
_recent_nonces: dict[str, tuple[str, float]] = {}

# ─── ユーティリティ ────────────────────────────────────────────────────────────
def _new_token() -> str:
    return secrets.token_urlsafe(32)

def _new_room_id() -> str:
    return secrets.token_urlsafe(24)

def _pem_to_der(pem_bytes: bytes) -> bytes:
    """PEM → DER（base64デコード）。フィンガープリント計算はDERで行う必要がある。"""
    import base64 as _b64
    b64 = ''.join(
        l for l in pem_bytes.decode('ascii').splitlines()
        if not l.startswith('-----')
    )
    return _b64.b64decode(b64)

def ensure_tls_cert() -> str:
    global _cert_fingerprint
    if CERT_PATH.exists() and KEY_PATH.exists():
        fp = hashlib.sha256(_pem_to_der(CERT_PATH.read_bytes())).hexdigest()
        _cert_fingerprint = fp
        return fp
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for openssl_bin in ["/usr/local/ssl/bin/openssl", "/usr/bin/openssl", "openssl"]:
        try:
            import subprocess
            subprocess.run([
                openssl_bin, "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(KEY_PATH),
                "-x509", "-days", "3650",
                "-out", str(CERT_PATH),
                "-subj", "/CN=ArcHiveServer",
            ], check=True, capture_output=True)
            fp = hashlib.sha256(_pem_to_der(CERT_PATH.read_bytes())).hexdigest()
            _cert_fingerprint = fp
            return fp
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    _logger.warning("TLS証明書の生成に失敗しました")
    _cert_fingerprint = ""
    return ""

def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        cfg.pop("username", None)
        cfg.pop("password", None)
    else:
        cfg = DEFAULT_CONFIG.copy()
    if "firebase" not in cfg or not isinstance(cfg.get("firebase"), dict):
        cfg["firebase"] = {}
    if "turn" not in cfg:
        cfg["turn"] = DEFAULT_CONFIG["turn"].copy()
    if "stun_servers" not in cfg:
        cfg["stun_servers"] = DEFAULT_CONFIG["stun_servers"].copy()
    changed = False
    if "token" in cfg and "devices" not in cfg:
        old_token = cfg.pop("token")
        cfg["devices"] = {}
        if old_token:
            cfg["devices"]["legacy-device"] = {
                "name":          "移行済み端末（旧トークン）",
                "token":         old_token,
                "status":        "approved",
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        changed = True
    if "devices" not in cfg:
        cfg["devices"] = {}
        changed = True
    if "browser-local" not in cfg["devices"]:
        cfg["devices"]["browser-local"] = {
            "name":          "ブラウザ（ローカル）",
            "token":         _new_token(),
            "status":        "approved",
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        changed = True
    if not cfg.get("room_id"):
        cfg["room_id"] = _new_room_id()
        changed = True
    if changed:
        save_config(cfg)
    return cfg

def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

def natural_key(s: str) -> list:
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]

def book_id(path: Path) -> str:
    return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]

def open_archive(path: Path):
    if path.suffix.lower() in ('.rar', '.cbr'):
        if not UNRAR_AVAILABLE:
            raise RuntimeError(
                f"RAR ファイルの読み込みには unrar が必要です: {UNRAR_PATH or '未検出'}"
            )
        return rarfile.RarFile(str(path))
    return zipfile.ZipFile(str(path))

def get_page_list(path: Path) -> list[str]:
    if path.is_dir():
        return sorted(
            [f.name for f in path.iterdir() if f.suffix.lower() in IMAGE_EXT],
            key=natural_key,
        )
    if path.suffix.lower() in FITZ_EXT:
        with pymupdf.open(str(path)) as doc:
            return [str(i) for i in range(len(doc))]
    with open_archive(path) as af:
        names = [n for n in af.namelist() if Path(n).suffix.lower() in IMAGE_EXT]
        return sorted(names, key=natural_key)

def read_raw_image(path: Path, page_id: str) -> bytes:
    if path.is_dir():
        return (path / page_id).read_bytes()
    if path.suffix.lower() in FITZ_EXT:
        with pymupdf.open(str(path)) as doc:
            pix = doc[int(page_id)].get_pixmap(matrix=pymupdf.Matrix(2, 2))
            return pix.tobytes("jpeg")
    with open_archive(path) as af:
        with af.open(page_id) as f:
            return f.read()

def resize_jpeg(data: bytes, max_side: int = PAGE_MAX, quality: int = 85) -> bytes:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return data

def scan_books(dirs: list[str]) -> int:
    global _books
    _books = {}
    scan_roots = [Path(d) for d in dirs if Path(d).exists()]
    multi = len(scan_roots) >= 2
    root_labels: dict[Path, str] = {}
    if multi:
        used: set[str] = set()
        for root in scan_roots:
            base = root.name or str(root).rstrip("/") or "(root)"
            label, i = base, 2
            while label in used:
                label = f"{base} ({i})"; i += 1
            used.add(label)
            root_labels[root] = label

    def _rel(root: Path, target: Path) -> str:
        sub = target.relative_to(root).parent.as_posix()
        if not multi:
            return sub
        label = root_labels[root]
        return label if sub == "." else f"{label}/{sub}"

    for root in scan_roots:
        img_dir_counts: dict[Path, int] = {}
        for f in sorted(root.rglob("*"), key=lambda p: [natural_key(x) for x in p.parts]):
            suf = f.suffix.lower()
            if suf in ARCHIVE_EXT:
                bid = book_id(f)
                _books[bid] = {"path": f, "title": f.stem, "rel": _rel(root, f)}
            elif suf in IMAGE_EXT:
                img_dir_counts[f.parent] = img_dir_counts.get(f.parent, 0) + 1
        for d, cnt in img_dir_counts.items():
            if cnt < MIN_FOLDER_IMAGES:
                continue
            bid = book_id(d)
            _books[bid] = {"path": d, "title": d.name, "rel": _rel(root, d)}
    return len(_books)

def _preload_covers_bg(book_ids: list[str]) -> None:
    global _preloading
    total = len(book_ids)
    cached_ids = {p.stem for p in CACHE_DIR.glob("*.jpg")} if CACHE_DIR.exists() else set()
    uncached = [bid for bid in book_ids if bid not in cached_ids]
    skipped = total - len(uncached)
    if skipped:
        _log_queue.put(f"[キャッシュ] {skipped} 冊はキャッシュ済み（スキップ）")
    done = skipped
    try:
        for bid in uncached:
            _bg_wait_while_active()
            info = _books.get(bid)
            if not info:
                done += 1
                continue
            try:
                if "pages" not in info:
                    info["pages"] = get_page_list(info["path"])
                if info["pages"]:
                    data = read_raw_image(info["path"], info["pages"][0])
                    img  = Image.open(io.BytesIO(data)).convert("RGB")
                    img.thumbnail((COVER_W, COVER_H), Image.LANCZOS)
                    buf  = io.BytesIO()
                    img.save(buf, "JPEG", quality=80)
                    _cover_put(bid, buf.getvalue())
            except Exception:
                pass
            done += 1
            if done % 100 == 0 or done == total:
                _log_queue.put(f"[キャッシュ] {done}/{total} 冊完了")
        _log_queue.put(f"[キャッシュ] 完了 ({CACHE_DIR})")
    finally:
        _preloading = False

def start_preload() -> None:
    global _preloading
    if _preloading:
        _log_queue.put("[キャッシュ] 生成は既に実行中です（スキップ）")
        return
    _preloading = True
    threading.Thread(
        target=_preload_covers_bg, args=(list(_books.keys()),), daemon=True
    ).start()

def _cover_path(bid: str) -> Path:
    return CACHE_DIR / f"{bid}.jpg"

def _cover_get(bid: str) -> bytes | None:
    p = _cover_path(bid)
    return p.read_bytes() if p.exists() else None

def _cover_put(bid: str, data: bytes) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cover_path(bid).write_bytes(data)

def _page_cache_path(bid: str, n: int) -> Path:
    return PAGE_CACHE_DIR / f"{bid}_{n}_{PAGE_MAX}.jpg"

_page_cache_writes = 0

def _page_cache_put(path: Path, data: bytes) -> None:
    global _page_cache_writes
    PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    _page_cache_writes += 1
    if _page_cache_writes % 200 == 0:
        _trim_page_cache()

def _trim_page_cache() -> None:
    try:
        files = sorted(PAGE_CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for p in files[: max(0, len(files) - PAGE_CACHE_MAX)]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass

WARM_MAX_CONCURRENT = 2
_warming:      set[str] = set()
_warming_lock           = threading.Lock()

def _warm_book_cache(bid: str) -> None:
    try:
        info = _books.get(bid)
        if not info:
            return
        if "pages" not in info:
            info["pages"] = get_page_list(info["path"])
        path  = info["path"]
        pages = info["pages"]
        is_fitz = (not path.is_dir()) and path.suffix.lower() in FITZ_EXT
        is_dir  = path.is_dir()
        doc = pymupdf.open(str(path)) if is_fitz else None
        af  = None if (is_fitz or is_dir) else open_archive(path)
        try:
            for n, page_id in enumerate(pages):
                cp = _page_cache_path(bid, n)
                if cp.exists():
                    continue
                _bg_wait_while_active()
                try:
                    if is_fitz:
                        pix = doc[int(page_id)].get_pixmap(matrix=pymupdf.Matrix(2, 2))
                        raw = pix.tobytes("jpeg")
                    elif is_dir:
                        raw = (path / page_id).read_bytes()
                    else:
                        with af.open(page_id) as f:
                            raw = f.read()
                    _page_cache_put(cp, resize_jpeg(raw))
                except Exception:
                    pass
        finally:
            if doc is not None:
                doc.close()
            if af is not None:
                af.close()
        _log_queue.put(f"[キャッシュ] 本文先読み完了: {info.get('title','')[:30]} ({len(pages)}p)")
    finally:
        with _warming_lock:
            _warming.discard(bid)

def start_warm(bid: str) -> None:
    with _warming_lock:
        if bid in _warming or len(_warming) >= WARM_MAX_CONCURRENT:
            return
        _warming.add(bid)
    threading.Thread(target=_warm_book_cache, args=(bid,), daemon=True).start()

# ─── LAN 自動発見 ─────────────────────────────────────────────────────────────
DISCOVERY_PORT  = 8770
DISCOVERY_PROBE = b"COMICSERVER_DISCOVER"
_discovery_started = False

def get_global_ipv6() -> str:
    """グローバルIPv6を返す（Linux版: ip -6 addr show で安定アドレスを探す）。"""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0].split("%")[0]
        s.close()
        if not ip or ip.startswith("fe80") or ip == "::1":
            return ""
        prefix = ip.split(":")[:4]
        try:
            out = subprocess.run(
                ["ip", "-6", "addr", "show"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if (
                    line.startswith("inet6 ")
                    and "scope global" in line
                    and "temporary" not in line
                    and "deprecated" not in line
                ):
                    addr = line.split()[1].split("/")[0]
                    if addr.split(":")[:4] == prefix:
                        return addr
        except Exception:
            pass
        return ip
    except OSError:
        return ""

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"

def _connection_info(requester_ip: str = "") -> dict:
    if requester_ip:
        cached = _recent_nonces.get(requester_ip)
        if cached:
            nonce, expiry = cached
            if time.time() < expiry and nonce in _reg_nonces:
                return {
                    "service": "comicserver", "name": socket.gethostname(),
                    "room_id": _config.get("room_id", ""),
                    "host": get_local_ip(), "port": int(_config.get("port", 8765)),
                    "ipv6": get_global_ipv6(), "ipv4_global": _external_ipv4,
                    "ipv4_port": _external_ipv4_port,
                    "reg_nonce": nonce,
                    "cert_fingerprint": _cert_fingerprint,
                }
    nonce = secrets.token_urlsafe(16)
    _reg_nonces[nonce] = time.time() + 300
    if requester_ip:
        _recent_nonces[requester_ip] = (nonce, time.time() + 60)
    now = time.time()
    for k in [k for k, v in _reg_nonces.items() if v < now]:
        del _reg_nonces[k]
    return {
        "service": "comicserver", "name": socket.gethostname(),
        "room_id": _config.get("room_id", ""),
        "host": get_local_ip(), "port": int(_config.get("port", 8765)),
        "ipv6": get_global_ipv6(), "ipv4_global": _external_ipv4,
        "ipv4_port": _external_ipv4_port,
        "reg_nonce": nonce,
        "cert_fingerprint": _cert_fingerprint,
    }

def _discovery_responder() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as e:
        _log_queue.put(f"[発見] UDP {DISCOVERY_PORT} を確保できません: {e}")
        return
    _log_queue.put(f"[発見] LAN自動発見を待機中（UDP {DISCOVERY_PORT}）")
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            break
        if data.strip() != DISCOVERY_PROBE:
            continue
        try:
            sock.sendto(json.dumps(_connection_info(addr[0])).encode("utf-8"), addr)
        except OSError:
            pass

def start_discovery_responder() -> None:
    global _discovery_started
    if _discovery_started:
        return
    _discovery_started = True
    threading.Thread(target=_discovery_responder, daemon=True).start()

def _placeholder_jpeg() -> bytes:
    img = Image.new("RGB", (COVER_W, COVER_H), color=(49, 50, 68))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()

_cover_errors: set[str] = set()

# ─── FastAPI アプリ ────────────────────────────────────────────────────────────
api = FastAPI(title="ArcHiveServer", docs_url=None, redoc_url=None)

def _extract_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return value.strip()
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value.strip()).decode("utf-8", "replace")
        except Exception:
            return ""
        return decoded.partition(":")[2]
    return ""

def _browser_token() -> str:
    return _config.get("devices", {}).get("browser-local", {}).get("token", "")

def _find_device_by_token(token: str) -> tuple[str, dict] | None:
    if not token:
        return None
    for did, d in _config.get("devices", {}).items():
        if d.get("status") == "approved" and d.get("token") and secrets.compare_digest(token, d["token"]):
            return did, d
    return None

_AUTH_FAIL_LIMIT    = 10
_AUTH_FAIL_WINDOW   = 300
_AUTH_BLOCK_SECONDS = 600

_auth_failures: dict[str, list[float]] = {}
_auth_blocked:  dict[str, float]       = {}
_auth_lock = threading.Lock()

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def _is_blocked(ip: str) -> int:
    with _auth_lock:
        until = _auth_blocked.get(ip, 0.0)
        if until <= time.time():
            _auth_blocked.pop(ip, None)
            return 0
        return int(until - time.time()) + 1

def _record_auth_failure(ip: str) -> None:
    now = time.time()
    with _auth_lock:
        fails = [t for t in _auth_failures.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
        fails.append(now)
        if len(fails) >= _AUTH_FAIL_LIMIT:
            _auth_blocked[ip] = now + _AUTH_BLOCK_SECONDS
            _auth_failures.pop(ip, None)
            _log_queue.put(
                f"[認証] {ip} を{_AUTH_BLOCK_SECONDS // 60}分間ブロックしました"
            )
        else:
            _auth_failures[ip] = fails

def _clear_auth_failures(ip: str) -> None:
    with _auth_lock:
        _auth_failures.pop(ip, None)
        _auth_blocked.pop(ip, None)

def _check_auth(request: Request) -> str:
    ip = _client_ip(request)
    supplied = (
        _extract_token(request.headers.get("authorization"))
        or request.query_params.get("token", "")
        or request.cookies.get("ms_token", "")
    )
    if supplied and _find_device_by_token(supplied):
        _clear_auth_failures(ip)
        return "ok"
    blocked_for = _is_blocked(ip)
    if blocked_for:
        raise HTTPException(
            status_code=429, detail="Too many failed attempts",
            headers={"Retry-After": str(blocked_for)},
        )
    if supplied:
        _record_auth_failure(ip)
    raise HTTPException(
        status_code=401, detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )

# ─── 本棚・リーダー UI（Android アプリ＋ブラウザ共通） ────────────────────────
@api.get("/")
def root(request: Request, _: str = Depends(_check_auth)):
    _html = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ArcHive</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1e1e2e;color:#cdd6f4;font-family:'Yu Gothic UI',sans-serif}
header{background:#181825;padding:10px 14px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:10;box-shadow:0 2px 8px #00000055;flex-wrap:wrap}
#logo{font-size:1.1em;color:#89b4fa;cursor:pointer;font-weight:bold;white-space:nowrap}
#logo:hover{color:#cba6f7}
#bread{font-size:12px;color:#a6adc8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
#bread button{background:#313244;border:none;color:#89b4fa;padding:2px 8px;border-radius:4px;cursor:pointer;font-size:12px;margin:0 2px}
#bread button:hover{background:#45475a}
#bread span.cur{color:#cdd6f4}
#search{width:200px;background:#313244;border:none;color:#cdd6f4;padding:6px 12px;border-radius:6px;font-size:13px;outline:none}
#search::placeholder{color:#585b70}
#cnt{color:#a6adc8;font-size:12px;white-space:nowrap}
#histbtn{background:#313244;border:none;color:#cba6f7;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap}
#histbtn:hover{background:#45475a}
#shelf{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;padding:12px;align-items:start}
.fc{background:#181825;border-radius:10px;padding:8px;cursor:pointer;transition:transform .15s,box-shadow .15s}
.fc:hover{transform:scale(1.04);box-shadow:0 4px 16px #00000077}
.pv{display:grid;grid-template-columns:1fr 1fr;gap:2px;border-radius:6px;overflow:hidden;aspect-ratio:4/3;background:#313244}
.pv img{width:100%;height:100%;object-fit:cover;display:block}
.pv.c1{grid-template-columns:1fr}
.fn{font-size:12px;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fct{font-size:10px;color:#585b70;margin-top:2px}
.bc{background:#181825;border-radius:8px;overflow:hidden;transition:transform .15s;cursor:pointer;position:relative}
.bc:hover{transform:scale(1.05)}
.cv{width:100%;aspect-ratio:5/7;object-fit:cover;display:block;background:#313244}
.tt{padding:4px 6px;font-size:10px;color:#a6adc8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdel{position:absolute;top:3px;right:3px;width:22px;height:22px;line-height:21px;text-align:center;background:#000a;color:#f38ba8;border-radius:50%;font-size:15px;font-weight:bold;z-index:3}
#msg{text-align:center;padding:60px;color:#585b70}
#reader{display:none;position:fixed;inset:0;background:#000;z-index:200;align-items:center;justify-content:center;overflow:hidden}
#rpa{overflow:hidden;width:100%;height:100%;position:relative}
#rpt{display:flex;width:300vw;height:100%;transform:translateX(-100vw);will-change:transform;touch-action:pan-y}
.rps{width:100vw;flex:0 0 100vw;display:flex;align-items:center;justify-content:center}
.rp{max-height:100vh;object-fit:contain;user-select:none}
.rp.single{max-width:100vw}
.rp.spread{max-width:50vw}
#rui{position:absolute;inset:0;pointer-events:none}
#rtop{position:absolute;top:0;left:0;right:0;padding:10px 14px;background:linear-gradient(#000a,transparent);display:flex;align-items:center;gap:8px;transition:opacity .3s}
#rtitle{color:#cdd6f4;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#rclose{background:none;border:none;color:#cdd6f4;font-size:22px;cursor:pointer;padding:0 4px;pointer-events:all}
#rbot{position:absolute;bottom:0;left:0;right:0;padding:8px 14px 12px;background:linear-gradient(transparent,#000a);display:flex;align-items:center;gap:8px;transition:opacity .3s;flex-wrap:wrap}
#rpg{color:#a6adc8;font-size:12px;min-width:60px;text-align:center}
#rslider{flex:1;accent-color:#89b4fa}
.rbtn{background:#313244;border:none;color:#cdd6f4;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;pointer-events:all}
.rbtn:hover{background:#45475a}
#rui.hidden #rtop,#rui.hidden #rbot{opacity:0;pointer-events:none}
</style>
</head>
<body>
<header>
<span id="logo" onclick="go('')">ArcHive</span>
<div id="bread"></div>
<input id="search" type="search" placeholder="検索..." oninput="doSearch(this.value)">
<span id="cnt"></span>
<button id="histbtn" onclick="showHist()">履歴</button>
</header>
<div id="shelf"><div id="msg">読み込み中...</div></div>
<div id="reader">
<div id="rpa">
<div id="rpt">
<div class="rps"><img class="rp" id="rp0"></div>
<div class="rps"><img class="rp" id="rp1"></div>
<div class="rps"><img class="rp" id="rp2"></div>
</div>
</div>
<div id="rui">
<div id="rtop"><span id="rtitle"></span><button id="rclose" onclick="closeReader()">✕</button></div>
<div id="rbot">
<button class="rbtn" onclick="prevPage()">◀</button>
<input id="rslider" type="range" min="0" value="0" oninput="jumpPage(+this.value)">
<button class="rbtn" onclick="nextPage()">▶</button>
<span id="rpg"></span>
<button class="rbtn" id="spreadbtn" onclick="toggleSpread()">見開き</button>
</div>
</div>
</div>
<script>
const tok=document.cookie.split(';').find(c=>c.trim().startsWith('ms_token='))?.split('=')[1]||'';
const H={'Authorization':'Bearer '+tok};
let allBooks=[],_hist=JSON.parse(localStorage.getItem('arch_hist')||'[]');
let _bid='',_pages=[],_cur=0,_spread=false,_rtl=true;
let _curPath='',_allFolders=[],_skipPush=false;
function dbg(m){console.log('[ArcHive]',m)}
function $id(i){return document.getElementById(i)}
async function apiFetch(url){const r=await fetch(url,{headers:H});if(!r.ok)throw r.status;return r.json();}
function fmtPath(p){if(!p)return'<span class="cur">本棚</span>';const parts=p.split('/');return parts.map((s,i)=>{const pp=parts.slice(0,i+1).join('/');return i<parts.length-1?`<button onclick="go('${pp}')">${s}</button>`:`<span class="cur">${s}</span>`;}).join(' / ');}
async function go(path){
  _curPath=path;
  $id('bread').innerHTML=fmtPath(path);
  const shelf=$id('shelf');shelf.innerHTML='<div id="msg">読み込み中...</div>';
  $id('search').value='';
  try{
    const d=await apiFetch('/api/folders?path='+encodeURIComponent(path));
    renderShelf(d.folders,d.books);
  }catch(e){shelf.innerHTML='<div id="msg">エラー: '+e+'</div>';}
  if(!_skipPush)history.pushState({v:'f',path},'');
}
function renderShelf(folders,books){
  const shelf=$id('shelf');
  shelf.innerHTML='';
  let cnt=folders.length+books.length;
  $id('cnt').textContent=cnt?cnt+'件':'';
  if(!cnt){shelf.innerHTML='<div id="msg">本が見つかりません</div>';return;}
  for(const f of folders){
    const el=document.createElement('div');el.className='fc';
    const pv=document.createElement('div');
    const ids=f.ids||[];
    pv.className='pv'+(ids.length<=1?' c1':'');
    for(const id of ids.slice(0,4)){const img=document.createElement('img');img.src='/api/books/'+id+'/cover';pv.appendChild(img);}
    el.appendChild(pv);
    const fn=document.createElement('div');fn.className='fn';fn.textContent=f.name;
    const fc=document.createElement('div');fc.className='fct';fc.textContent=f.count+'冊';
    el.appendChild(fn);el.appendChild(fc);
    el.onclick=()=>go(f.path);
    shelf.appendChild(el);
  }
  for(const b of books){
    const el=document.createElement('div');el.className='bc';
    const img=document.createElement('img');img.className='cv';img.src='/api/books/'+b.id+'/cover';el.appendChild(img);
    const tt=document.createElement('div');tt.className='tt';tt.textContent=b.title;el.appendChild(tt);
    const hdel=document.createElement('span');hdel.className='hdel';hdel.textContent='×';
    hdel.onclick=(e)=>{e.stopPropagation();removeHist(b.id);};el.appendChild(hdel);
    el.onclick=()=>openBook(b.id,b.title);
    shelf.appendChild(el);
  }
}
function doSearch(q){
  if(!q){go(_curPath);return;}
  const ql=q.toLowerCase();
  const hits=allBooks.filter(b=>b.title.toLowerCase().includes(ql));
  $id('bread').innerHTML='<span class="cur">検索: '+q+'</span>';
  renderShelf([],hits);
}
function showHist(){
  const books=_hist.map(id=>allBooks.find(b=>b.id===id)).filter(Boolean);
  $id('bread').innerHTML='<span class="cur">最近読んだ本</span>';
  $id('shelf').innerHTML='';
  renderShelf([],books);
}
function addHist(id){_hist=_hist.filter(i=>i!==id);_hist.unshift(id);if(_hist.length>30)_hist.length=30;localStorage.setItem('arch_hist',JSON.stringify(_hist));}
function removeHist(id){_hist=_hist.filter(i=>i!==id);localStorage.setItem('arch_hist',JSON.stringify(_hist));}
async function openBook(bid,title){
  _bid=bid;
  try{
    const info=await apiFetch('/api/books/'+bid+'/info');
    _pages=Array.from({length:info.count},(_,i)=>i);
  }catch(e){alert('読み込みエラー: '+e);return;}
  addHist(bid);
  const saved=JSON.parse(localStorage.getItem('pos_'+bid)||'null');
  _cur=saved||0;
  if(_cur>=_pages.length)_cur=0;
  $id('rtitle').textContent=title;
  $id('rslider').max=_pages.length-1;
  $id('reader').style.display='flex';
  document.body.style.overflow='hidden';
  loadPage(_cur);
  history.pushState({v:'r',bid,title},'');
}
function closeReader(){
  $id('reader').style.display='none';
  document.body.style.overflow='';
  history.back();
}
function pageUrl(n){return '/api/books/'+_bid+'/pages/'+n;}
function loadPage(n){
  if(n<0||n>=_pages.length)return;
  _cur=n;
  localStorage.setItem('pos_'+_bid,n);
  $id('rslider').value=n;
  $id('rpg').textContent=(n+1)+'/'+_pages.length;
  const l=_rtl?((n+1)<_pages.length?n+1:n):Math.max(0,n-1);
  const r=_rtl?Math.max(0,n-1):((n+1)<_pages.length?n+1:n);
  $id('rp0').src=pageUrl(l);
  $id('rp1').src=pageUrl(n);
  $id('rp2').src=pageUrl(r);
  $id('rpt').style.transform='translateX(-100vw)';
}
function nextPage(){if(_rtl?_cur>0:_cur<_pages.length-1)loadPage(_rtl?_cur-1:_cur+1);}
function prevPage(){if(_rtl?_cur<_pages.length-1:_cur>0)loadPage(_rtl?_cur+1:_cur-1);}
function jumpPage(n){loadPage(n);}
function toggleSpread(){_spread=!_spread;$id('spreadbtn').textContent=_spread?'単ページ':'見開き';document.querySelectorAll('.rp').forEach(el=>el.className='rp '+(_spread?'spread':'single'));}
let _ts=0,_tx=0;
$id('rpa').addEventListener('touchstart',e=>{_ts=e.touches[0].clientX;_tx=0;});
$id('rpa').addEventListener('touchmove',e=>{_tx=e.touches[0].clientX-_ts;});
$id('rpa').addEventListener('touchend',()=>{if(Math.abs(_tx)>40)(_tx<0?nextPage:prevPage)();});
$id('rpa').addEventListener('click',e=>{
  const x=e.clientX/window.innerWidth;
  if(x<0.25)prevPage();else if(x>0.75)nextPage();
  else{$id('rui').classList.toggle('hidden');}
});
window.addEventListener('keydown',e=>{if($id('reader').style.display==='flex'){if(e.key==='ArrowRight')nextPage();if(e.key==='ArrowLeft')prevPage();if(e.key==='Escape')closeReader();}});
window.addEventListener('popstate',e=>{
  const st=e.state;
  if(!st||st.v==='f'){$id('reader').style.display='none';document.body.style.overflow='';go(st?.path||'');}
});
async function init(){
  history.replaceState({v:'f',path:''},'');
  _skipPush=true;await go('');_skipPush=false;
  fetch('/api/books',{headers:H}).then(r=>r.json()).then(d=>{allBooks=d;});
}
init();
</script>
</body>
</html>"""
    resp = HTMLResponse(_html, headers={"Cache-Control": "no-store"})
    qtoken = request.query_params.get("token")
    if qtoken:
        resp.set_cookie("ms_token", qtoken, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 365)
    return resp

# ─── 端末登録・承認 API ────────────────────────────────────────────────────────
@api.post("/api/devices/register")
async def register_device(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    reg_nonce   = str(body.get("reg_nonce",   ""))
    device_id   = str(body.get("device_id",   ""))[:64]
    device_name = str(body.get("device_name", "Android"))[:50]
    if not device_id:
        raise HTTPException(400, "device_id required")
    if reg_nonce not in _reg_nonces or time.time() > _reg_nonces[reg_nonce]:
        _reg_nonces.pop(reg_nonce, None)
        raise HTTPException(403, "Invalid or expired registration nonce")
    del _reg_nonces[reg_nonce]
    devices = _config.setdefault("devices", {})
    if device_id in devices and devices[device_id].get("status") == "approved":
        _log_queue.put(f"[認証] 既承認端末が再ペアリング: {device_name}")
        return {"status": "already_approved", "token": devices[device_id].get("token", "")}
    reg_token = secrets.token_urlsafe(32)
    devices[device_id] = {
        "name":         device_name,
        "status":       "pending",
        "reg_token":    reg_token,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_config(_config)
    _log_queue.put(f"[認証] 新端末が接続要求中: {device_name} — /admin で承認してください")
    return {"status": "pending", "reg_token": reg_token}

@api.get("/api/devices/status")
async def device_status(request: Request):
    ip = _client_ip(request)
    reg_token = request.query_params.get("reg_token", "")
    if not reg_token:
        raise HTTPException(400, "reg_token required")
    for device_id, device in _config.get("devices", {}).items():
        rt = device.get("reg_token", "")
        if not rt:
            continue
        try:
            match = secrets.compare_digest(reg_token, rt)
        except Exception:
            continue
        if not match:
            continue
        _clear_auth_failures(ip)
        status = device.get("status", "pending")
        if status == "pending":
            return {"status": "pending"}
        if status == "approved":
            return {"status": "approved", "token": device.get("token", "")}
        raise HTTPException(403, "Device revoked")
    blocked_for = _is_blocked(ip)
    if blocked_for:
        raise HTTPException(status_code=429, detail="Too many failed attempts",
                             headers={"Retry-After": str(blocked_for)})
    _record_auth_failure(ip)
    raise HTTPException(404, "Not found")

def _conn_type(ip: str) -> str:
    if ip in ("127.0.0.1", "::1", "unknown"):
        return "ローカル"
    try:
        addr = ipaddress.ip_address(ip)
        if addr.ipv4_mapped:
            ip = str(addr.ipv4_mapped)
            addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            own_v6 = get_global_ipv6()
            if own_v6 and ip.split(":")[:4] == own_v6.split(":")[:4]:
                return "LAN(IPv6)"
            return "IPv6直結"
        if _is_global_ipv4(ip):
            return "IPv4外部"
    except Exception:
        pass
    return "LAN"

_http_conn_log: dict[str, str] = {}

@api.get("/api/status")
def status(request: Request, _: str = Depends(_check_auth)):
    ip = _client_ip(request)
    conn_type = _conn_type(ip)
    if _http_conn_log.get(ip) != conn_type:
        _http_conn_log[ip] = conn_type
        _log_queue.put(f"[HTTP] 接続: {ip} ({conn_type})")
    return {"books": len(_books), "unrar": UNRAR_AVAILABLE, "version": "1.0"}

@api.get("/api/books")
def list_books(_: str = Depends(_check_auth)):
    _note_user_activity()
    return [
        {"id": bid, "title": info["title"], "rel": info.get("rel", ".")}
        for bid, info in _books.items()
    ]

@api.get("/api/folders")
def list_folders(path: str = "", _: str = Depends(_check_auth)):
    _note_user_activity()
    subfolders:   dict[str, dict] = {}
    direct_books: list[dict]      = []
    for bid, info in _books.items():
        rel = info.get("rel", ".")
        if path == "":
            if rel == ".":
                direct_books.append({"id": bid, "title": info["title"]})
            else:
                top = rel.split("/")[0]
                if top not in subfolders:
                    subfolders[top] = {"name": top, "path": top, "count": 0, "ids": []}
                subfolders[top]["count"] += 1
                if len(subfolders[top]["ids"]) < 4:
                    subfolders[top]["ids"].append(bid)
        else:
            if rel == path:
                direct_books.append({"id": bid, "title": info["title"]})
            elif rel.startswith(path + "/"):
                rest = rel[len(path) + 1:]
                sub  = rest.split("/")[0]
                full = path + "/" + sub
                if sub not in subfolders:
                    subfolders[sub] = {"name": sub, "path": full, "count": 0, "ids": []}
                subfolders[sub]["count"] += 1
                if len(subfolders[sub]["ids"]) < 4:
                    subfolders[sub]["ids"].append(bid)
    return {
        "path":    path,
        "folders": sorted(subfolders.values(), key=lambda x: natural_key(x["name"])),
        "books":   sorted(direct_books,        key=lambda b: natural_key(b["title"])),
    }

@api.get("/api/books/{bid}/info")
def book_info(bid: str, _: str = Depends(_check_auth)):
    _note_user_activity()
    info = _books.get(bid)
    if not info:
        raise HTTPException(404, "Book not found")
    if "pages" not in info:
        info["pages"] = get_page_list(info["path"])
    start_warm(bid)
    return {"id": bid, "title": info["title"], "count": len(info["pages"])}

@api.get("/api/connection-info")
def api_connection_info(_: str = Depends(_check_auth)):
    return _connection_info()

@api.get("/api/books/{bid}/cover")
def book_cover(bid: str, _: str = Depends(_check_auth)):
    _note_user_activity()
    cached = _cover_get(bid)
    if cached:
        return Response(cached, media_type="image/jpeg")
    info = _books.get(bid)
    if not info:
        return Response(_placeholder_jpeg(), media_type="image/jpeg")
    try:
        if "pages" not in info:
            info["pages"] = get_page_list(info["path"])
        if not info["pages"]:
            return Response(_placeholder_jpeg(), media_type="image/jpeg")
        data = read_raw_image(info["path"], info["pages"][0])
        img  = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((COVER_W, COVER_H), Image.LANCZOS)
        buf  = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        _cover_put(bid, buf.getvalue())
        return Response(buf.getvalue(), media_type="image/jpeg")
    except Exception as e:
        key = f"{type(e).__name__}:{str(e)[:80]}"
        if key not in _cover_errors:
            _cover_errors.add(key)
            _log_queue.put(f"[WARN] 表紙取得失敗 ({info.get('title','')[:30]}): {e}")
        return Response(_placeholder_jpeg(), media_type="image/jpeg")

@api.get("/api/books/{bid}/pages/{n}")
def get_page(bid: str, n: int, _: str = Depends(_check_auth)):
    _note_user_activity()
    info = _books.get(bid)
    if not info:
        raise HTTPException(404, "Book not found")
    if "pages" not in info:
        info["pages"] = get_page_list(info["path"])
    if n < 0 or n >= len(info["pages"]):
        raise HTTPException(404, f"Page {n} out of range")
    cache_path = _page_cache_path(bid, n)
    if cache_path.exists():
        try:
            return Response(cache_path.read_bytes(), media_type="image/jpeg")
        except OSError:
            pass
    try:
        data = read_raw_image(info["path"], info["pages"][n])
        jpeg = resize_jpeg(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    _page_cache_put(cache_path, jpeg)
    return Response(jpeg, media_type="image/jpeg")

@api.post("/api/scan")
def api_scan(_: str = Depends(_check_auth)):
    n = scan_books(_config.get("scan_dirs", []))
    return {"books": n}

# ─── /admin 管理ページ ─────────────────────────────────────────────────────────

_ADMIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ArcHive 管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1e1e2e;color:#cdd6f4;font-family:'Yu Gothic UI',sans-serif;padding:0}
.top{background:#181825;padding:12px 20px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px #00000055}
.top h1{font-size:1.1em;color:#89b4fa}
.top a{color:#a6adc8;font-size:13px;text-decoration:none}
.top a:hover{color:#89b4fa}
main{max-width:900px;margin:0 auto;padding:20px}
section{background:#181825;border-radius:10px;padding:16px;margin-bottom:16px}
h2{font-size:1em;color:#89b4fa;margin-bottom:12px;border-bottom:1px solid #313244;padding-bottom:8px}
.info-row{display:flex;gap:8px;margin-bottom:6px;align-items:baseline;flex-wrap:wrap}
.label{color:#a6adc8;font-size:12px;min-width:100px}
.val{color:#cdd6f4;font-size:13px;font-family:monospace}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:bold}
.badge.ok{background:#a6e3a120;color:#a6e3a1}
.badge.warn{background:#f9e2af20;color:#f9e2af}
.badge.ng{background:#f38ba820;color:#f38ba8}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#a6adc8;font-weight:normal;padding:6px 8px;border-bottom:1px solid #313244}
td{padding:6px 8px;border-bottom:1px solid #31324455;vertical-align:middle}
.act{display:flex;gap:6px}
button.approve{background:#a6e3a120;color:#a6e3a1;border:1px solid #a6e3a140;padding:3px 10px;border-radius:6px;cursor:pointer;font-size:12px}
button.approve:hover{background:#a6e3a130}
button.revoke{background:#f38ba820;color:#f38ba8;border:1px solid #f38ba840;padding:3px 10px;border-radius:6px;cursor:pointer;font-size:12px}
button.revoke:hover{background:#f38ba830}
button.rescan{background:#89b4fa20;color:#89b4fa;border:1px solid #89b4fa40;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:13px}
button.rescan:hover{background:#89b4fa30}
.chip{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px}
.chip.approved{background:#a6e3a120;color:#a6e3a1}
.chip.pending{background:#f9e2af20;color:#f9e2af}
.chip.revoked{background:#f38ba820;color:#f38ba8}
.url{color:#89b4fa;font-size:12px;font-family:monospace;word-break:break-all}
#msg{font-size:12px;color:#f9e2af;margin-left:8px;display:none}
ul.dirs{list-style:none;font-size:13px;font-family:monospace}
ul.dirs li{padding:4px 0;color:#cba6f7;border-bottom:1px solid #31324444}
ul.dirs li:last-child{border:none}
</style>
</head>
<body>
<div class="top">
  <h1>ArcHive 管理</h1>
  <a href="/" id="shelf_link">本棚を開く →</a>
</div>
<main id="main">読み込み中...</main>
<script>
async function load(){
  const r=await fetch('/admin/api');
  if(!r.ok){document.getElementById('main').textContent='認証エラー: /admin/api にアクセスできません';return;}
  const d=await r.json();
  const port=d.port||8765;
  const proto=location.protocol;
  const token=d.browser_token||'';
  document.getElementById('shelf_link').href='/?token='+token;

  let devices_html='<tr><td colspan=4 style="color:#585b70;text-align:center">登録端末なし</td></tr>';
  if(d.devices&&d.devices.length){
    devices_html=d.devices.map(dev=>`
      <tr>
        <td>${esc(dev.name)}</td>
        <td><span class="chip ${dev.status}">${dev.status==='approved'?'承認済み':dev.status==='pending'?'承認待ち':'取り消し'}</span></td>
        <td style="color:#585b70;font-size:11px">${esc(dev.requested_at||dev.registered_at||'')}</td>
        <td class="act">
          ${dev.status!=='approved'?`<button class="approve" onclick="approve('${esc(dev.id)}')">承認</button>`:''}
          ${dev.status!=='revoked'?`<button class="revoke" onclick="revoke('${esc(dev.id)}')">取り消し</button>`:''}
        </td>
      </tr>`).join('');
  }

  const dirs_html=d.scan_dirs&&d.scan_dirs.length
    ?d.scan_dirs.map(p=>`<li>${esc(p)}</li>`).join('')
    :'<li style="color:#585b70">スキャンフォルダ未設定（config.json を編集してください）</li>';

  document.getElementById('main').innerHTML=`
    <section>
      <h2>サーバー状態</h2>
      <div class="info-row"><span class="label">状態</span><span class="badge ok">稼働中</span></div>
      <div class="info-row"><span class="label">本の冊数</span><span class="val">${d.books}冊</span></div>
      <div class="info-row"><span class="label">unrar</span><span class="badge ${d.unrar?'ok':'warn'}">${d.unrar?'利用可能':'未インストール（RARは開けません）'}</span></div>
      <div class="info-row"><span class="label">LAN URL</span><span class="url">${proto}//${d.local_ip}:${port}/</span></div>
      ${d.ipv6?`<div class="info-row"><span class="label">IPv6</span><span class="url">${proto}//[${d.ipv6}]:${port}/</span></div>`:''}
      ${d.ipv4_global?`<div class="info-row"><span class="label">IPv4外部</span><span class="url">${proto}//${d.ipv4_global}:${d.ipv4_port||port}/</span></div>`:''}
    </section>
    <section>
      <h2>スキャンフォルダ</h2>
      <ul class="dirs">${dirs_html}</ul>
      <div style="margin-top:12px;display:flex;align-items:center">
        <button class="rescan" onclick="rescan()">再スキャン</button>
        <span id="msg"></span>
      </div>
    </section>
    <section>
      <h2>登録端末</h2>
      <table><thead><tr><th>端末名</th><th>状態</th><th>日時</th><th>操作</th></tr></thead>
      <tbody id="devtbl">${devices_html}</tbody></table>
    </section>
    <section>
      <h2>接続トークン（ブラウザ用）</h2>
      <div class="info-row"><span class="label">トークン</span><span class="val">${esc(token)}</span></div>
      <div class="info-row" style="margin-top:4px"><span class="label">本棚URL</span><span class="url">${proto}//${location.host}/?token=${esc(token)}</span></div>
    </section>
  `;
}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function approve(id){
  const r=await fetch('/admin/approve/'+encodeURIComponent(id),{method:'POST'});
  if(r.ok)load();else alert('失敗: '+(await r.text()));
}
async function revoke(id){
  if(!confirm('端末のアクセスを取り消しますか？'))return;
  const r=await fetch('/admin/revoke/'+encodeURIComponent(id),{method:'POST'});
  if(r.ok)load();else alert('失敗: '+(await r.text()));
}
async function rescan(){
  const btn=document.querySelector('.rescan');btn.disabled=true;btn.textContent='スキャン中...';
  const msg=document.getElementById('msg');msg.style.display='inline';msg.textContent='再スキャン中...';
  try{
    const r=await fetch('/admin/rescan',{method:'POST'});
    const d=await r.json();
    msg.textContent=`完了: ${d.books}冊`;
  }catch(e){msg.textContent='エラー: '+e;}
  btn.disabled=false;btn.textContent='再スキャン';
  setTimeout(()=>{msg.style.display='none';},4000);
}
load();
</script>
</body>
</html>"""

@api.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(_ADMIN_HTML, headers={"Cache-Control": "no-store"})

@api.get("/admin/api")
def admin_api():
    devices_list = []
    for did, d in _config.get("devices", {}).items():
        if did == "browser-local":
            continue
        devices_list.append({
            "id":           did,
            "name":         d.get("name", did),
            "status":       d.get("status", "pending"),
            "registered_at": d.get("registered_at", ""),
            "requested_at":  d.get("requested_at", ""),
        })
    return {
        "books":        len(_books),
        "unrar":        UNRAR_AVAILABLE,
        "port":         int(_config.get("port", 8765)),
        "local_ip":     get_local_ip(),
        "ipv6":         get_global_ipv6(),
        "ipv4_global":  _external_ipv4,
        "ipv4_port":    _external_ipv4_port,
        "scan_dirs":    _config.get("scan_dirs", []),
        "browser_token": _browser_token(),
        "devices":      devices_list,
    }

@api.post("/admin/approve/{device_key}")
def admin_approve(device_key: str):
    devices = _config.get("devices", {})
    if device_key not in devices:
        raise HTTPException(404, "Device not found")
    dev = devices[device_key]
    if dev.get("status") == "approved":
        return {"status": "already_approved"}
    token = _new_token()
    dev["status"] = "approved"
    dev["token"]  = token
    dev["registered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_config(_config)
    _log_queue.put(f"[認証] 端末を承認しました: {dev.get('name', device_key)}")
    return {"status": "approved"}

@api.post("/admin/revoke/{device_key}")
def admin_revoke(device_key: str):
    devices = _config.get("devices", {})
    if device_key not in devices:
        raise HTTPException(404, "Device not found")
    devices[device_key]["status"] = "revoked"
    devices[device_key].pop("token", None)
    save_config(_config)
    _log_queue.put(f"[認証] 端末アクセスを取り消しました: {devices[device_key].get('name', device_key)}")
    return {"status": "revoked"}

@api.post("/admin/rescan")
def admin_rescan():
    n = scan_books(_config.get("scan_dirs", []))
    start_preload()
    return {"books": n}

# ─── UPnP（IPv4/IPv6 ポート自動開放） ─────────────────────────────────────────
_external_ipv4:      str = ""
_external_ipv4_port: int = 0
_upnp_v4: dict           = {"url": "", "stype": ""}
_upnp_fresh_add_done: bool = False

def _is_global_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_global
    except Exception:
        return False

def _upnp_ssdp_discover(
    service_type: str = "urn:schemas-upnp-org:service:WANIPv6FirewallControl:1",
    timeout: float = 2.0,
) -> str:
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {service_type}\r\n"
        "\r\n"
    ).encode()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            lan_ip = get_local_ip()
            if lan_ip and not lan_ip.startswith("169.254"):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(lan_ip))
                sock.bind((lan_ip, 0))
        except OSError:
            pass
        try:
            sock.sendto(msg, ("239.255.255.250", 1900))
            while True:
                try:
                    data, _ = sock.recvfrom(65507)
                    for line in data.decode(errors="replace").split("\r\n"):
                        if line.upper().startswith("LOCATION:"):
                            return line.split(":", 1)[1].strip()
                except OSError:
                    break
        finally:
            sock.close()
    except Exception:
        pass
    return ""

def _upnp_get_control_url(
    location: str, service_match: str = "WANIPv6FirewallControl",
) -> tuple[str, str, str]:
    import urllib.request as _ur
    import xml.etree.ElementTree as ET
    try:
        with _ur.urlopen(location, timeout=5) as r:
            xml_data = r.read()
    except Exception:
        return "", "", ""
    from urllib.parse import urlparse
    parsed = urlparse(location)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return "", "", ""
    for svc in root.iter():
        if not svc.tag.endswith("service"):
            continue
        stype_el  = next((c for c in svc if c.tag.endswith("serviceType")),  None)
        ctrl_el   = next((c for c in svc if c.tag.endswith("controlURL")),    None)
        if stype_el is None or ctrl_el is None:
            continue
        stype = (stype_el.text or "").strip()
        ctrl  = (ctrl_el.text  or "").strip()
        if service_match in stype and ctrl:
            return ctrl, base_url, stype
    return "", "", ""

def _upnp_add_pinhole(base_url: str, ctrl_path: str, ipv6: str, port: int) -> None:
    import urllib.request as _ur
    ctrl_url = base_url + ctrl_path if ctrl_path.startswith("/") else base_url + "/" + ctrl_path
    svc = "urn:schemas-upnp-org:service:WANIPv6FirewallControl:1"
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:AddPinhole xmlns:u="{svc}">'
        "<RemoteHost></RemoteHost><RemoteHostPort>0</RemoteHostPort>"
        "<Protocol>6</Protocol>"
        f"<InternalPort>{port}</InternalPort>"
        f"<InternalClient>{ipv6}</InternalClient>"
        "<LeaseTime>3600</LeaseTime>"
        "</u:AddPinhole>"
        "</s:Body></s:Envelope>"
    ).encode()
    req = _ur.Request(
        ctrl_url, data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{svc}#AddPinhole"',
        },
    )
    with _ur.urlopen(req, timeout=5):
        pass

def _upnp_open_ipv6(ipv6: str, port: int) -> bool:
    try:
        location = _upnp_ssdp_discover()
        if not location:
            _log_queue.put("[IPv6] UPnP: IPv6ファイアウォール制御対応ルーターが見つかりません")
            return False
        ctrl, base, _ = _upnp_get_control_url(location)
        if not ctrl:
            _log_queue.put("[IPv6] UPnP: ルーターがIPv6ピンホールに非対応です")
            return False
        _upnp_add_pinhole(base, ctrl, ipv6, port)
        _log_queue.put(f"[IPv6] UPnP: TCPポート {port} を自動開放しました")
        return True
    except Exception as e:
        _log_queue.put(f"[IPv6] UPnP: 失敗 ({e})")
        return False

def _upnp_fault(resp: str):
    m = re.search(r"<errorCode[^>]*>(\w+)</errorCode>", resp)
    if not m:
        return None
    d = re.search(r"<errorDescription[^>]*>(.*?)</errorDescription>", resp)
    return (m.group(1), (d.group(1).strip() if d else ""))

def _upnp_soap(url: str, service_type: str, action: str, body_args: str = "") -> str:
    import urllib.request as _ur
    import urllib.error  as _ue
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{body_args}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    req = _ur.Request(
        url, data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction":   f'"{service_type}#{action}"',
        },
    )
    try:
        with _ur.urlopen(req, timeout=5) as r:
            return r.read().decode(errors="replace")
    except _ue.HTTPError as e:
        try:
            return e.read().decode(errors="replace")
        except Exception:
            return f"<errorCode>HTTP{e.code}</errorCode>"

def _upnp_discover_wan_ipv4() -> tuple[str, str]:
    attempts = [
        ("urn:schemas-upnp-org:service:WANPPPConnection:1", "WANPPPConnection"),
        ("urn:schemas-upnp-org:service:WANIPConnection:1",  "WANIPConnection"),
        ("urn:schemas-upnp-org:device:InternetGatewayDevice:1", "WANPPPConnection"),
        ("urn:schemas-upnp-org:device:InternetGatewayDevice:1", "WANIPConnection"),
    ]
    for st, match in attempts:
        location = _upnp_ssdp_discover(st)
        if not location:
            continue
        ctrl, base, stype = _upnp_get_control_url(location, match)
        if ctrl:
            url = base + ctrl if ctrl.startswith("/") else base + "/" + ctrl
            return url, stype
    return "", ""

def _pcp_gateway_ip(control_url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(control_url).hostname or ""
        socket.inet_aton(host)
        return host
    except Exception:
        return ""

def _pcp_map(gateway_ip: str, internal_ip: str, internal_port: int,
             lifetime: int = 7200) -> tuple[str, int]:
    PCP_PORT  = 5351
    PROTO_TCP = 6
    try:
        client_ip128 = b'\x00' * 10 + b'\xff\xff' + socket.inet_aton(internal_ip)
    except OSError:
        return ("", 0)
    nonce  = os.urandom(12)
    header = struct.pack("!BBHI16s", 2, 1, 0, lifetime, client_ip128)
    map_body = struct.pack(
        "!12sB3sHH16s",
        nonce, PROTO_TCP, b'\x00\x00\x00',
        internal_port, 0, b'\x00' * 16,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.sendto(header + map_body, (gateway_ip, PCP_PORT))
        data, _ = sock.recvfrom(1100)
    except (socket.timeout, OSError):
        return ("", 0)
    finally:
        sock.close()
    if len(data) < 60 or data[0] != 2 or data[1] != 0x81 or data[3] != 0:
        return ("", 0)
    if data[24:36] != nonce:
        return ("", 0)
    assigned_ext_port = struct.unpack_from("!H", data, 42)[0]
    raw_ip = data[44:60]
    if not assigned_ext_port:
        return ("", 0)
    if raw_ip[:12] == b'\x00' * 10 + b'\xff\xff':
        try:
            ext_ip = socket.inet_ntoa(raw_ip[12:])
        except OSError:
            ext_ip = ""
    else:
        try:
            ext_ip = socket.inet_ntop(socket.AF_INET6, raw_ip)
        except OSError:
            ext_ip = ""
    return (ext_ip, assigned_ext_port)

def _upnp_open_ipv4(internal_ip: str, port: int) -> str:
    global _external_ipv4, _external_ipv4_port
    if not _config.get("upnp_ipv4_open", True):
        return ""
    try:
        if not _upnp_v4["url"]:
            url, stype = _upnp_discover_wan_ipv4()
            if not url:
                _log_queue.put("[IPv4] UPnP: IPv4ポート開放対応ルーターが見つかりません")
                return ""
            _upnp_v4["url"], _upnp_v4["stype"] = url, stype
        url, stype = _upnp_v4["url"], _upnp_v4["stype"]
        resp = _upnp_soap(url, stype, "GetExternalIPAddress")
        fault = _upnp_fault(resp)
        if fault:
            _external_ipv4 = ""; _external_ipv4_port = 0
            return ""
        m   = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", resp)
        ext = m.group(1).strip() if m else ""
        if not ext or not _is_global_ipv4(ext):
            _external_ipv4 = ""; _external_ipv4_port = 0
            return ""

        def _add(ep: int, lease: int) -> str:
            return _upnp_soap(
                url, stype, "AddPortMapping",
                "<NewRemoteHost></NewRemoteHost>"
                f"<NewExternalPort>{ep}</NewExternalPort>"
                "<NewProtocol>TCP</NewProtocol>"
                f"<NewInternalPort>{port}</NewInternalPort>"
                f"<NewInternalClient>{internal_ip}</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>ArcHive</NewPortMappingDescription>"
                f"<NewLeaseDuration>{lease}</NewLeaseDuration>")

        def _del(ep: int) -> None:
            _upnp_soap(url, stype, "DeletePortMapping",
                       "<NewRemoteHost></NewRemoteHost>"
                       f"<NewExternalPort>{ep}</NewExternalPort>"
                       "<NewProtocol>TCP</NewProtocol>")

        def _mapped(ep: int) -> bool:
            chk = _upnp_soap(url, stype, "GetSpecificPortMappingEntry",
                             "<NewRemoteHost></NewRemoteHost>"
                             f"<NewExternalPort>{ep}</NewExternalPort>"
                             "<NewProtocol>TCP</NewProtocol>")
            return _upnp_fault(chk) is None

        def _try_open(ep: int):
            resp2 = _add(ep, 0)
            f = _upnp_fault(resp2)
            if f and f[0] == "718":
                if _mapped(ep):
                    return (True, None)
                try:
                    _del(ep)
                except Exception:
                    pass
                resp2 = _add(ep, 0)
                f = _upnp_fault(resp2)
            if f and f[0] != "718":
                resp2 = _add(ep, 3600)
                f = _upnp_fault(resp2)
            if f:
                return (False, f)
            return (_mapped(ep), None)

        chosen      = 0
        last_fault  = None
        prev        = int(_config.get("upnp_external_port", 0) or 0)

        global _upnp_fresh_add_done
        if prev:
            if not _upnp_fresh_add_done:
                _upnp_fresh_add_done = True
                try:
                    _del(prev)
                except Exception:
                    pass
            resp2 = _add(prev, 0)
            f = _upnp_fault(resp2)
            if f is not None and f[0] != "718":
                resp2 = _add(prev, 3600)
                f = _upnp_fault(resp2)
            if f is None or f[0] == "718":
                chosen = prev
            else:
                last_fault = f

        if not chosen:
            cands: list[int] = [port]
            if prev:
                for n in (49, 50, 51, 48):
                    p = n * 1024 + (prev % 1024)
                    if 1024 <= p <= 65535 and p not in (port, prev):
                        cands.append(p)
            base = 49152 + (port % 16000)
            for off in (0, 277):
                p = base + off
                cands.append(p - 16000 if p > 65535 else p)
            seen: set[int] = set()
            cands = [p for p in cands if p and p != prev and not (p in seen or seen.add(p))]
            for ep in cands:
                ok, f = _try_open(ep)
                if ok:
                    chosen = ep; break
                if f:
                    last_fault = f

        if not chosen and last_fault and last_fault[0] == "718":
            gw = _pcp_gateway_ip(url)
            if gw:
                pcp_ext_ip, pcp_port = _pcp_map(gw, internal_ip, port)
                if pcp_port:
                    chosen = pcp_port
                    if _is_global_ipv4(pcp_ext_ip):
                        ext = pcp_ext_ip
                    _log_queue.put(f"[IPv4] PCP: 外部ポート {pcp_port} を取得 ({ext}:{pcp_port})")

        if not chosen and last_fault and last_fault[0] == "718":
            for residue_start in range(0, 1024, 16):
                sweep_port = 49 * 1024 + residue_start
                ok, f = _try_open(sweep_port)
                if ok:
                    chosen = sweep_port
                    _log_queue.put(f"[IPv4] MAP-E: 外部ポート {sweep_port} を開放 ({ext}:{sweep_port})")
                    break
                if f and f[0] != "718":
                    break

        if not chosen:
            _external_ipv4 = ""; _external_ipv4_port = 0
            return ""

        if _config.get("upnp_external_port") != chosen:
            _config["upnp_external_port"] = chosen
            save_config(_config)

        if (ext, chosen) != (_external_ipv4, _external_ipv4_port):
            _log_queue.put(f"[IPv4] UPnP: 外部ポート {chosen} を開放しました（外部 {ext}:{chosen}）")
        _external_ipv4 = ext
        _external_ipv4_port = chosen
        return ext
    except Exception as e:
        _log_queue.put(f"[IPv4] UPnP: 失敗 ({e})")
        _upnp_v4["url"] = ""
        _external_ipv4 = ""; _external_ipv4_port = 0
        return ""

# ─── IPv6/IPv4 監視スレッド（UPnP ポート維持・60秒ループ） ──────────────────────
_ipv6_monitor_running: bool = False

def _ipv6_monitor_thread() -> None:
    global _ipv6_monitor_running
    port = int(_config.get("port", 8765))
    last_ipv6 = ""
    while _ipv6_monitor_running:
        ipv6 = get_global_ipv6()
        if ipv6 and ipv6 != last_ipv6:
            _upnp_open_ipv6(ipv6, port)
            last_ipv6 = ipv6
        _upnp_open_ipv4(get_local_ip(), port)
        for _ in range(60):
            if not _ipv6_monitor_running:
                break
            time.sleep(1)

def start_ipv6_monitor() -> None:
    global _ipv6_monitor_running
    if _ipv6_monitor_running:
        return
    _ipv6_monitor_running = True
    threading.Thread(target=_ipv6_monitor_thread, daemon=True).start()

# ─── エントリポイント ─────────────────────────────────────────────────────────
def main() -> None:
    global _config

    # シグナルハンドラ（systemd の SIGTERM に対応）
    def _shutdown(signum, _frame):
        global _ipv6_monitor_running
        _logger.info("シャットダウン中...")
        _ipv6_monitor_running = False
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    APP_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    _config = load_config()

    fp = ensure_tls_cert()
    _logger.info(f"TLS 証明書フィンガープリント: {fp[:16]}...")

    dirs = _config.get("scan_dirs", [])
    if not dirs:
        _logger.warning(
            f"スキャンフォルダが未設定です。"
            f"{CONFIG_PATH} の scan_dirs を設定してください。"
        )

    n = scan_books(dirs)
    _logger.info(f"本棚スキャン完了: {n}冊")

    start_preload()
    start_discovery_responder()
    start_ipv6_monitor()

    host = _config.get("host", "0.0.0.0")
    port = int(_config.get("port", 8765))
    browser_url = f"https://{get_local_ip()}:{port}/?token={_browser_token()}"
    admin_url   = f"http://{get_local_ip()}:{port}/admin"

    _logger.info(f"ArcHive サーバー起動: https://{host}:{port}/")
    _logger.info(f"管理ページ: {admin_url}")
    _logger.info(f"本棚（ブラウザ）: {browser_url}")

    # IPv4(LAN) と IPv6(外部) を同時に受け付けるデュアルスタックソケットを試みる。
    # Python asyncio は IPv6 ソケットに IPV6_V6ONLY=1 を強制するため、
    # socket.create_server(dualstack_ipv6=True) で明示的に 0 にする必要がある。
    _sock = None
    if host in ("::", "0.0.0.0"):
        try:
            import socket as _sock_mod
            _sock = _sock_mod.create_server(
                ('::', port),
                family=_sock_mod.AF_INET6,
                dualstack_ipv6=True,
                reuse_port=False,
            )
            _logger.info(f"デュアルスタック(IPv4+IPv6) ソケット起動: port {port}")
        except Exception as _e:
            _logger.warning(f"デュアルスタック失敗、{host} にフォールバック: {_e}")
            _sock = None

    if _sock is not None:
        uvicorn.run(
            api,
            fd=_sock.fileno(),
            ssl_certfile=str(CERT_PATH),
            ssl_keyfile=str(KEY_PATH),
            log_config=None,
        )
    else:
        uvicorn.run(
            api,
            host=host,
            port=port,
            ssl_certfile=str(CERT_PATH),
            ssl_keyfile=str(KEY_PATH),
            log_config=None,
        )

if __name__ == "__main__":
    main()
