"""
manga_server_app.py - 自炊ファイル配信サーバー（GUI版）
ZIP / RAR / CBZ / PDF / EPUB をスキャンして FastAPI + uvicorn で配信する
Android クライアントから Basic 認証で接続して閲覧できる
"""
import sys
sys.path.insert(0, r"C:\keiri_python\python_embed\Lib\site-packages")

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

import rarfile
import pymupdf
from PIL import Image
try:
    import pystray
    from PIL import ImageDraw
    _TRAY_AVAILABLE = True
except Exception:
    _TRAY_AVAILABLE = False   # pystray 未導入でもサーバー機能は動く（トレイ格納だけ無効）
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import Response, HTMLResponse
import secrets
import base64
import uvicorn
import datetime

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import load_pem_x509_certificate

# ─── 定数 ────────────────────────────────────────────────────────────────────
UNRAR_PATH      = r"C:\Program Files\WinRAR\UnRAR.exe"
UNRAR_AVAILABLE = Path(UNRAR_PATH).exists()
if UNRAR_AVAILABLE:
    rarfile.UNRAR_TOOL = UNRAR_PATH

IMAGE_EXT    = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
FITZ_EXT     = {'.pdf', '.epub'}   # PyMuPDF(fitz) でページをレンダリングして配信する形式
ARCHIVE_EXT  = {'.zip', '.rar', '.cbz', '.cbr', '.pdf', '.epub'}
MIN_FOLDER_IMAGES = 3   # この枚数以上の画像が直置きされたフォルダは「1冊の本」とみなす
COVER_W, COVER_H = 200, 280
PAGE_MAX     = 1800   # 長辺の最大ピクセル（スマホ向けリサイズ）

CONFIG_PATH = Path(__file__).parent / "manga_server_config.json"
ICON_PATH   = Path(__file__).parent / "assets" / "icon" / "app_icon.ico"
TRAY_ICON_PATH = Path(__file__).parent / "assets" / "icon" / "tray_icon.png"
_TLS_DIR    = Path(__file__).parent          # 証明書をスクリプトと同じフォルダに置く
CERT_PATH   = _TLS_DIR / "server.crt"
KEY_PATH    = _TLS_DIR / "server.key"
CACHE_DIR   = Path.home() / ".manga_server" / "cache"  # ローカルに保存（NAS越しI/O回避）
PAGE_CACHE_DIR = CACHE_DIR / "pages"   # リサイズ済み本文ページのディスクキャッシュ
PAGE_CACHE_MAX = 4000                  # 本文ページキャッシュの最大ファイル数（超過分は古い順に削除）

# ─── Firebase（シグナリング）開発者設定 ─────────────────────────────────────────
# 設計: 開発者の1プロジェクトを全ユーザー共通の"伝言板"にする。サーバーは匿名認証の
# 一般クライアントとして繋ぐ。api_key はクライアント鍵で秘匿情報ではない（アプリにも
# 同梱される）ため、ここ（＝全配布コピー共通の既定値）に直接書いてよい。
# 開発者は↓の2つを一度だけ記入すれば、配布した全サーバーが同じ伝言板を使う。
# （個別に変えたい場合のみ manga_server_config.json の "firebase" で上書き可能）
DEFAULT_FIREBASE: dict = {
    "api_key":      "AIzaSyCHz05od7Ta6wJFKSWcTisWfhJh_kg1fKQ",
    "database_url": "https://comicserver-default-rtdb.asia-southeast1.firebasedatabase.app",
}

DEFAULT_CONFIG: dict = {
    "scan_dirs":    [],
    "room_id":      "",   # WebRTC部屋ID（初回生成。LANペアリングでアプリに渡す）
    "port":         8765,
    "host":         "0.0.0.0",
    "on_close":     "ask",   # ウィンドウ×ボタン押下時: "ask"(毎回確認)/"exit"/"tray"
    "on_minimize":  "ask",   # 最小化ボタン押下時:     "ask"(毎回確認)/"minimize"/"tray"
    "firebase":     {},   # 空＝DEFAULT_FIREBASEを使う。値を入れればそれで上書き
    "stun_servers": ["stun:stun.l.google.com:19302"],
    "turn": {               # 任意: STUNで繋がらない環境用。空のままでもOK
        "url":        "",
        "username":   "",
        "credential": "",
    },
}

def _new_token() -> str:
    """推測不能な認証トークンを生成（URLセーフ・約43文字）。"""
    return secrets.token_urlsafe(32)

def _new_room_id() -> str:
    """WebRTC部屋IDを生成（URLセーフ・約32文字）。"""
    return secrets.token_urlsafe(24)

# ─── TLS 自己署名証明書 ───────────────────────────────────────────────────────
_cert_fingerprint: str = ""   # SHA-256(DER) hex。起動時に ensure_tls_cert() で設定される

def ensure_tls_cert() -> str:
    """自己署名EC証明書が無ければ生成し、SHA-256フィンガープリント(hex)を返す。"""
    global _cert_fingerprint
    if CERT_PATH.exists() and KEY_PATH.exists():
        try:
            cert = load_pem_x509_certificate(CERT_PATH.read_bytes())
            fp = cert.fingerprint(hashes.SHA256()).hex()
            _cert_fingerprint = fp
            return fp
        except Exception:
            pass  # 壊れていれば再生成
    # 新規生成
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ComicServer")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    fp = cert.fingerprint(hashes.SHA256()).hex()
    _cert_fingerprint = fp
    return fp

# ─── グローバル状態（GUI ↔ API 共有） ─────────────────────────────────────────
_books: dict[str, dict] = {}    # book_id -> {path, title, folder, pages}
_preloading: bool       = False # キャッシュ生成スレッドが走っているか
_config: dict           = {}
_log_queue: queue.Queue = queue.Queue()

# 端末登録ノンス（メモリのみ・サーバー再起動でリセット）
_reg_nonces:    dict[str, float]               = {}  # nonce -> expiry (time.time())
_recent_nonces: dict[str, tuple[str, float]]   = {}  # client_ip -> (nonce, expiry)

# ─── ユーティリティ ────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        # 旧版の固定ユーザー名/パスワードは廃止（トークン認証へ一本化）
        cfg.pop("username", None)
        cfg.pop("password", None)
    else:
        cfg = DEFAULT_CONFIG.copy()
    # firebase は空dictがデフォルト（_fb_cfgがDEFAULT_FIREBASEへフォールバックする）
    if "firebase" not in cfg or not isinstance(cfg.get("firebase"), dict):
        cfg["firebase"] = {}
    if "turn" not in cfg:
        cfg["turn"] = DEFAULT_CONFIG["turn"].copy()
    if "stun_servers" not in cfg:
        cfg["stun_servers"] = DEFAULT_CONFIG["stun_servers"].copy()

    # トークン未設定（新規インストール or 旧版からの移行）なら生成して保存
    changed = False

    # 旧版（単一 token）→ 端末別トークンへのマイグレーション
    if "token" in cfg and "devices" not in cfg:
        old_token = cfg.pop("token")
        cfg["devices"] = {}
        if old_token:
            cfg["devices"]["legacy-device"] = {
                "name":           "移行済み端末（旧トークン）",
                "token":          old_token,
                "status":         "approved",
                "registered_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        changed = True

    if "devices" not in cfg:
        cfg["devices"] = {}
        changed = True

    # ブラウザ用デバイス（PCの「ブラウザで開く」専用・常に承認済み）
    if "browser-local" not in cfg["devices"]:
        cfg["devices"]["browser-local"] = {
            "name":          "PC ブラウザ（ローカル）",
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
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def natural_key(s: str) -> list:
    """'第99巻' → ['第', 99, '巻'] のように数字を整数化して自然順ソートに使う"""
    return [int(c) if c.isdigit() else c.lower()
            for c in re.split(r'(\d+)', s)]

def book_id(path: Path) -> str:
    return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]

def open_archive(path: Path):
    if path.suffix.lower() in ('.rar', '.cbr'):
        if not UNRAR_AVAILABLE:
            raise RuntimeError(
                "RAR ファイルの読み込みには WinRAR が必要です。\n"
                f"インストールされていないか、場所が異なります: {UNRAR_PATH}"
            )
        return rarfile.RarFile(path, 'r')
    return zipfile.ZipFile(path, 'r')

def get_page_list(path: Path) -> list[str]:
    """アーカイブ内の画像ファイル名（またはPDFページ番号・画像直置きフォルダのファイル名）一覧を返す"""
    if path.is_dir():
        # 画像直置きフォルダ: 直下の画像を自然順で並べる
        return sorted((c.name for c in path.iterdir()
                       if c.is_file() and c.suffix.lower() in IMAGE_EXT),
                      key=natural_key)
    if path.suffix.lower() in FITZ_EXT:
        doc = pymupdf.open(str(path))
        try:
            return [str(i) for i in range(len(doc))]
        finally:
            doc.close()
    with open_archive(path) as af:
        return sorted(f for f in af.namelist()
                      if Path(f).suffix.lower() in IMAGE_EXT)

def read_raw_image(path: Path, page_id: str) -> bytes:
    """指定ページの画像データを bytes で返す"""
    if path.is_dir():
        return (path / page_id).read_bytes()
    if path.suffix.lower() in FITZ_EXT:
        doc = pymupdf.open(str(path))
        try:
            pix = doc[int(page_id)].get_pixmap(matrix=pymupdf.Matrix(2, 2))
            return pix.tobytes("jpeg")
        finally:
            doc.close()
    with open_archive(path) as af:
        with af.open(page_id) as f:
            return f.read()

def resize_jpeg(data: bytes, max_side: int = PAGE_MAX, quality: int = 85) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        r = max_side / max(w, h)
        img = img.resize((int(w * r), int(h * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()

def scan_books(dirs: list[str]) -> int:
    global _books
    _books = {}
    scan_roots = [Path(d) for d in dirs if Path(d).exists()]

    # スキャンフォルダが2つ以上のときは、各ルートをトップ階層のフォルダとして見せる。
    # （ルート1個のときは従来どおりルート直下をそのまま本棚に並べる）
    multi = len(scan_roots) >= 2
    root_labels: dict[Path, str] = {}
    if multi:
        used: set[str] = set()
        for root in scan_roots:
            base = root.name or str(root).replace("\\", "/").rstrip("/") or "(root)"
            label, i = base, 2
            while label in used:          # 別ドライブの同名フォルダ等は連番で区別
                label = f"{base} ({i})"; i += 1
            used.add(label)
            root_labels[root] = label

    def _rel(root: Path, target: Path) -> str:
        """スキャンルートからの相対フォルダパス（"." / "作品A" / "ジャンル1/作品A"）。
        複数ルート時は先頭にルートのラベルを付ける。"""
        sub = target.relative_to(root).parent.as_posix()
        if not multi:
            return sub
        label = root_labels[root]
        return label if sub == "." else f"{label}/{sub}"

    for root in scan_roots:
        img_dir_counts: dict[Path, int] = {}  # 画像が直置きされたフォルダ -> 直下の画像枚数
        for f in sorted(root.rglob("*"), key=lambda p: [natural_key(x) for x in p.parts]):
            suf = f.suffix.lower()
            if suf in ARCHIVE_EXT:
                bid = book_id(f)
                _books[bid] = {"path": f, "title": f.stem, "rel": _rel(root, f)}
            elif suf in IMAGE_EXT:
                img_dir_counts[f.parent] = img_dir_counts.get(f.parent, 0) + 1
        # アーカイブ化されておらず画像が直置きされたフォルダを1冊の本として登録
        for d, cnt in img_dir_counts.items():
            if cnt < MIN_FOLDER_IMAGES:
                continue   # 表紙画像など数枚だけのフォルダは本扱いしない
            bid = book_id(d)
            _books[bid] = {"path": d, "title": d.name, "rel": _rel(root, d)}
    return len(_books)

def _preload_covers_bg(book_ids: list[str]) -> None:
    """全表紙をディスクにキャッシュ保存。既存ファイルはスキップして途中再開できる。"""
    global _preloading
    total = len(book_ids)
    # ディレクトリを一括スキャンして set を作る（ファイルごとの exists() × N を避ける）
    cached_ids = {p.stem for p in CACHE_DIR.glob("*.jpg")} if CACHE_DIR.exists() else set()
    uncached   = [bid for bid in book_ids if bid not in cached_ids]
    skipped    = total - len(uncached)
    if skipped:
        _log_queue.put(f"[キャッシュ] {skipped} 冊はキャッシュ済み（スキップ）")
    done = skipped
    try:
        for bid in uncached:
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

# ─── 本文ページのディスクキャッシュ ───────────────────────────────────────────
def _page_cache_path(bid: str, n: int) -> Path:
    # PAGE_MAX を含めることでリサイズ設定を変えた時に古いキャッシュと混ざらない
    return PAGE_CACHE_DIR / f"{bid}_{n}_{PAGE_MAX}.jpg"

_page_cache_writes = 0  # 数百回に1回だけ掃除するためのカウンタ

def _page_cache_put(path: Path, data: bytes) -> None:
    """一時ファイル→アトミック rename で保存（同時アクセスでの破損・競合を回避）。"""
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
    """キャッシュ数が上限を超えたら、更新時刻の古いファイルから削除する。"""
    try:
        files = sorted(PAGE_CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for p in files[: max(0, len(files) - PAGE_CACHE_MAX)]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass

# ─── 本文ページのバックグラウンド先読み（本を開いた瞬間に全ページを温める） ───
WARM_MAX_CONCURRENT = 2          # 同時に温める本の最大数（CPU占有を防ぐ）
_warming: set[str]    = set()    # 現在温め中の book_id
_warming_lock         = threading.Lock()

def _warm_book_cache(bid: str) -> None:
    """書庫を1回だけ開き、未キャッシュのページだけを順に生成・保存する（既存はスキップ）。"""
    try:
        info = _books.get(bid)
        if not info:
            return
        if "pages" not in info:
            info["pages"] = get_page_list(info["path"])
        path  = info["path"]
        pages = info["pages"]
        is_fitz = (not path.is_dir()) and path.suffix.lower() in FITZ_EXT
        is_dir = path.is_dir()
        doc = pymupdf.open(str(path)) if is_fitz else None
        af  = None if (is_fitz or is_dir) else open_archive(path)
        try:
            for n, page_id in enumerate(pages):
                cp = _page_cache_path(bid, n)
                if cp.exists():
                    continue   # オンデマンドや前回温めで既に生成済み
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
                    pass   # 1ページ失敗しても続行
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
    """本を開いた合図でバックグラウンド先読みを開始（多重起動・過負荷を防ぐ）。"""
    with _warming_lock:
        if bid in _warming or len(_warming) >= WARM_MAX_CONCURRENT:
            return
        _warming.add(bid)
    threading.Thread(target=_warm_book_cache, args=(bid,), daemon=True).start()

# ─── LAN自動発見（UDPブロードキャスト応答） ──────────────────────────────────
# アプリが LAN にブロードキャストした探索パケットに、このサーバーの接続情報を返す。
# TCPポート（変更可）とは独立した固定UDPポートで待つので、ポートを変えても発見できる。
DISCOVERY_PORT     = 8770
DISCOVERY_PROBE    = b"COMICSERVER_DISCOVER"
_discovery_started = False

def get_global_ipv6() -> str:
    """インターネットへ出る際のグローバルIPv6を返す（無ければ空文字）。
    getsockname()はOSのプライバシー拡張で一時アドレス(数時間で失効・ローテーション)を
    返すことが多いため、同じNIC・同じ/64内にPublic(安定)アドレスがあればそちらを優先する
    （PCにWi-Fi/有線など複数NICがあり、それぞれが別アドレスを持つ場合があるため、
    getsockname()が属するインターフェースのブロック内に限定して探す）。
    """
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))   # 実際には送信しない（経路確認のみ）
        ip = s.getsockname()[0]
        s.close()
        if not ip or ip.startswith("fe80") or ip == "::1":
            return ""
        ip = ip.split("%")[0]
        prefix = ip.split(":")[:4]
        try:
            out = subprocess.run(
                ["netsh", "interface", "ipv6", "show", "address"],
                capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="replace",
            ).stdout
            for block in out.split("\n\n"):
                public_addr = None
                contains_ip = False
                for line in block.splitlines():
                    cols = line.split()
                    if len(cols) == 5 and cols[0] in ("Public", "Temporary"):
                        addr = cols[4].split("%")[0]
                        if cols[0] == "Public" and addr.split(":")[:4] == prefix:
                            public_addr = addr
                        if addr == ip:
                            contains_ip = True
                if contains_ip and public_addr:
                    return public_addr
        except Exception:
            pass
        return ip
    except OSError:
        pass
    return ""

def _connection_info(requester_ip: str = "") -> dict:
    """LAN発見応答・接続情報。端末登録用ノンスを発行する（共有トークンは渡さない）。"""
    # 同一IPからの3連続探索パケットには同じノンスを返す（同一セッション）
    if requester_ip:
        cached = _recent_nonces.get(requester_ip)
        if cached:
            nonce, expiry = cached
            if time.time() < expiry and nonce in _reg_nonces:
                return {
                    "service": "comicserver", "name": socket.gethostname(),
                    "room_id": _config.get("room_id", ""),
                    "host": get_local_ip(), "port": int(_config.get("port", 8765)),
                    "ipv6": get_global_ipv6(), "reg_nonce": nonce,
                    "cert_fingerprint": _cert_fingerprint,
                }
    nonce = secrets.token_urlsafe(16)
    _reg_nonces[nonce] = time.time() + 300  # 5分有効
    if requester_ip:
        _recent_nonces[requester_ip] = (nonce, time.time() + 60)
    # 期限切れノンスを掃除
    now = time.time()
    for k in [k for k, v in _reg_nonces.items() if v < now]:
        del _reg_nonces[k]
    return {
        "service": "comicserver", "name": socket.gethostname(),
        "room_id": _config.get("room_id", ""),
        "host": get_local_ip(), "port": int(_config.get("port", 8765)),
        "ipv6": get_global_ipv6(), "reg_nonce": nonce,
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
    """発見応答スレッドを起動（多重起動防止）。サーバー再起動後も生かしたまま。"""
    global _discovery_started
    if _discovery_started:
        return
    _discovery_started = True
    threading.Thread(target=_discovery_responder, daemon=True).start()

def _placeholder_jpeg() -> bytes:
    """表紙取得失敗時に返すグレーのダミー画像"""
    img = Image.new("RGB", (COVER_W, COVER_H), color=(49, 50, 68))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()

_cover_errors: set[str] = set()  # 同じエラーを1回だけログに出すため

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

# ─── FastAPI アプリ ────────────────────────────────────────────────────────────
api       = FastAPI(title="MangaServer", docs_url=None, redoc_url=None)

def _extract_token(authorization: str | None) -> str:
    """Authorization ヘッダからトークンを取り出す（2方式に対応）。
    - `Bearer <token>`           : アプリが送る形式
    - `Basic base64(任意:token)` : ブラウザ内蔵ビューワーのBasic認証ダイアログ用。
      ユーザー名は無視し、パスワード部をトークンとして扱う。
    """
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
        return decoded.partition(":")[2]   # password 部
    return ""

def _browser_token() -> str:
    """PC ブラウザ用デバイスのトークンを返す（「ブラウザで開く」ボタン専用）。"""
    return _config.get("devices", {}).get("browser-local", {}).get("token", "")

def _find_device_by_token(token: str) -> tuple[str, dict] | None:
    """承認済み端末からトークンが一致するものを探す。(device_id, device情報) または None。"""
    if not token:
        return None
    for did, d in _config.get("devices", {}).items():
        if d.get("status") == "approved" and d.get("token") and secrets.compare_digest(token, d["token"]):
            return did, d
    return None

# ─── ブルートフォース対策（IPごとに誤トークン回数を数えて一時ブロック） ──────────────
_AUTH_FAIL_LIMIT    = 10   # この期間内にこの回数誤ったらブロック
_AUTH_FAIL_WINDOW   = 300  # 失敗カウントの期間（秒）
_AUTH_BLOCK_SECONDS = 600  # ブロック時間（秒）

_auth_failures: dict[str, list[float]] = {}  # ip -> 失敗時刻のリスト（メモリのみ・再起動でリセット）
_auth_blocked:  dict[str, float]       = {}  # ip -> ブロック解除時刻 (time.time())
_auth_lock = threading.Lock()

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def _is_blocked(ip: str) -> int:
    """ブロック中なら残り秒数（切り上げ・最低1）を返す。ブロックされていなければ0。"""
    with _auth_lock:
        until = _auth_blocked.get(ip, 0.0)
        if until <= time.time():
            _auth_blocked.pop(ip, None)
            return 0
        return int(until - time.time()) + 1

def _record_auth_failure(ip: str) -> None:
    """誤トークン/不正リクエストを記録し、規定回数を超えたら一定時間ブロックする。"""
    now = time.time()
    with _auth_lock:
        fails = [t for t in _auth_failures.get(ip, []) if now - t < _AUTH_FAIL_WINDOW]
        fails.append(now)
        if len(fails) >= _AUTH_FAIL_LIMIT:
            _auth_blocked[ip] = now + _AUTH_BLOCK_SECONDS
            _auth_failures.pop(ip, None)
            _log_queue.put(
                f"[認証] {ip} を{_AUTH_BLOCK_SECONDS // 60}分間ブロックしました"
                f"（{_AUTH_FAIL_WINDOW // 60}分以内に認証失敗{_AUTH_FAIL_LIMIT}回）"
            )
        else:
            _auth_failures[ip] = fails

def _clear_auth_failures(ip: str) -> None:
    """正規トークンでの認証成功時に呼ぶ。誤カウントとブロックを両方解除する。"""
    with _auth_lock:
        _auth_failures.pop(ip, None)
        _auth_blocked.pop(ip, None)

def _check_auth(request: Request) -> str:
    """全API共通の認証。承認済み端末のトークンのいずれかと一致すればOK。
      トークンの受け取り方:
        1. Authorization ヘッダ（アプリ=Bearer / ブラウザ=Basic）
        2. クエリ ?token=...（GUIの「ブラウザで開く」起動用。初回アクセスでCookie化）
        3. Cookie ms_token（一度トークン付きで開いた後の <img> 等）
      ブルートフォース対策: 正しいトークンは常に通す（再ペアリング直後の正規端末を
      ブロックで巻き込まない）。誤トークンが規定回数を超えたIPは一時ブロックする
      （未提示=未ログインの通常アクセスは失敗カウントしない）。
    """
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

@api.get("/")
def root(request: Request, _: str = Depends(_check_auth)):
    _html = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MangaServer</title>
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
/* ── リーダー ── */
#reader{display:none;position:fixed;inset:0;background:#000;z-index:200;align-items:center;justify-content:center;overflow:hidden}
#rpa{overflow:hidden;width:100%;height:100%;position:relative}
#rpt{display:flex;width:300vw;height:100%;transform:translateX(-100vw);will-change:transform;touch-action:pan-y}
.rps{width:100vw;flex:0 0 100vw;display:flex;align-items:center;justify-content:center}
.rp{max-height:100vh;object-fit:contain;user-select:none}
.rp.single{max-width:100vw}
.rp.spread{max-width:50vw}
#rmag{display:none;position:absolute;border:2px solid #89b4fa;border-radius:8px;pointer-events:none;z-index:5;background-repeat:no-repeat;box-shadow:0 2px 14px #000a}
#rzl{position:absolute;left:0;top:0;width:30%;height:80%;cursor:pointer;z-index:1}
#rzc{position:absolute;left:30%;top:10%;width:40%;height:80%;cursor:default;z-index:1}
#rzr{position:absolute;right:0;top:0;width:30%;height:80%;cursor:pointer;z-index:1}
#rui-top{display:none;position:absolute;top:0;left:0;right:0;height:20%;background:linear-gradient(#000c,transparent);align-items:center;gap:8px;padding:12px 16px;z-index:2}
#rui-bot{display:none;position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,#000d);flex-direction:column;align-items:center;padding:6px 0 10px;gap:6px;z-index:2}
.rbtn{background:rgba(30,30,46,.9);border:1px solid #45475a;color:#cdd6f4;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap}
.rbtn:hover{background:rgba(255,255,255,.12)}
.rbtn.on{border-color:#89b4fa;color:#89b4fa}
#rtitle-d{flex:1;color:#cdd6f4;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
#rpager-d{color:#a6adc8;font-size:12px;white-space:nowrap}
#rslider{flex:1;accent-color:#89b4fa;min-width:0}
/* フィルムストリップ */
#rfilm-wrap{width:100%;overflow-x:auto;scroll-behavior:smooth}
#rfilm-wrap::-webkit-scrollbar{height:4px}
#rfilm-wrap::-webkit-scrollbar-thumb{background:#45475a;border-radius:2px}
#rfilm{display:flex;gap:4px;padding:6px 14px;align-items:flex-end;min-height:222px}
.rft{flex:0 0 144px;cursor:pointer;border:2px solid transparent;border-radius:4px;overflow:hidden;background:#313244;transition:border-color .1s}
.rft img{width:144px;height:198px;object-fit:cover;display:block}
.rft.cur{border-color:#89b4fa}
.rft:hover{border-color:#585b70}
</style>
</head>
<body>
<header>
  <span id="logo" onclick="go('')">MangaServer</span>
  <span id="bread"></span>
  <button id="histbtn" onclick="showHistory()">📖 続き/履歴</button>
  <input id="search" type="text" placeholder="タイトル検索..." oninput="doSearch()">
  <span id="cnt"></span>
</header>
<div id="msg">読み込み中...<br><small id="dbg" style="color:#585b70;font-size:11px"></small></div>
<div id="shelf"></div>

<div id="reader">
  <div id="rpa">
    <div id="rpt">
      <div class="rps" id="rsp"><img class="rp" alt=""><img class="rp" alt=""></div>
      <div class="rps" id="rsc"><img class="rp" alt=""><img class="rp" alt=""></div>
      <div class="rps" id="rsn"><img class="rp" alt=""><img class="rp" alt=""></div>
    </div>
  </div>
  <div id="rmag"></div>
  <div id="rzl" onclick="rLeftClick()"></div>
  <div id="rzc" onclick="rToggleUI()"></div>
  <div id="rzr" onclick="rRightClick()"></div>
  <div id="rui-top">
    <button class="rbtn" onclick="rClose()">← 本棚</button>
    <span id="rtitle-d"></span>
    <button class="rbtn on" id="rbtn-dir" onclick="rToggleDir()">RTL ←</button>
    <button class="rbtn" id="rbtn-spr" onclick="rToggleSpread()">単ページ</button>
    <button class="rbtn" id="rbtn-fs" onclick="rToggleFS()">⛶ 全画面</button>
    <button class="rbtn" onclick="rHideUI()" style="margin-left:auto">✕</button>
  </div>
  <div id="rui-bot">
    <div id="rfilm-wrap"><div id="rfilm"></div></div>
    <div style="display:flex;align-items:center;gap:10px;width:96%;max-width:1200px">
      <input id="rslider" type="range" min="0" value="0"
             oninput="rSeekInput(this.value)" onchange="rSeekChange(this.value)">
      <span id="rpager-d"></span>
    </div>
    <div id="rvol-nav" style="display:none;gap:16px">
      <button class="rbtn" id="rbtn-pvol" onclick="rNavVol(-1)">◀ 前の巻</button>
      <button class="rbtn" id="rbtn-nvol" onclick="rNavVol(+1)">次の巻 ▶</button>
    </div>
  </div>
</div>
<script>
const dbg=s=>{document.getElementById('dbg').textContent=s;};
let allBooks=[], curPath='', searching=false;
let _folderBooks=[];  // 現在表示中フォルダの本リスト（巻ナビ用）
const x=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

// パスへ遷移
async function go(path){
  if(!_skipPush) history.pushState({v:'f', path}, '');
  if(searching){document.getElementById('search').value='';searching=false;}
  curPath=path;
  document.getElementById('cnt').textContent='読み込み中...';
  updateBread(path);
  dbg('fetch開始...');
  try{
    const r=await fetch('/api/folders?path='+encodeURIComponent(path));
    dbg('HTTP '+r.status);
    if(!r.ok){
      document.getElementById('msg').textContent='APIエラー: HTTP '+r.status;
      return;
    }
    const d=await r.json();
    dbg('JSON受信 folders='+JSON.stringify(d.folders?.length));
    if(!Array.isArray(d.folders)){
      document.getElementById('msg').textContent='レスポンス異常: '+JSON.stringify(d).slice(0,200);
      return;
    }
    render(d.folders, d.books||[]);
  }catch(e){
    document.getElementById('msg').textContent='接続エラー: '+e;
    dbg('catch: '+e);
  }
}

function render(folders, books){
  document.getElementById('msg').style.display='none';
  const fc=folders.map(f=>{
    const cls='c'+Math.min(f.ids.length,4);
    const imgs=f.ids.slice(0,4).map(id=>`<img src="/api/books/${x(id)}/cover" loading="lazy">`).join('');
    return `<div class="fc" data-path="${x(f.path)}" onclick="go(this.dataset.path)">
      <div class="pv ${cls}">${imgs}</div>
      <div class="fn">${x(f.name)}</div>
      <div class="fct">${f.count} 冊</div></div>`;
  }).join('');
  const bc=books.map(b=>
    `<div class="bc" title="${x(b.title)}" data-id="${x(b.id)}" data-title="${x(b.title)}" onclick="rOpen(this.dataset.id,this.dataset.title)">
      <img class="cv" src="/api/books/${x(b.id)}/cover" loading="lazy">
      <div class="tt">${x(b.title)}</div></div>`
  ).join('');
  document.getElementById('shelf').innerHTML=fc+bc;
  _folderBooks=books;  // 同フォルダ内の本リストを巻ナビ用に保持
  document.getElementById('cnt').textContent=
    (folders.length?folders.length+' フォルダ':'')+(folders.length&&books.length?' / ':'')+
    (books.length?books.length+' 冊':'');
}

function updateBread(path){
  if(!path){document.getElementById('bread').innerHTML='';return;}
  const parts=path.split('/');
  let html='<button data-path="" onclick="go(this.dataset.path)">⌂ 本棚</button>';
  let acc='';
  parts.forEach((p,i)=>{
    acc=acc?acc+'/'+p:p;
    html+=' / '+(i<parts.length-1
      ?`<button data-path="${x(acc)}" onclick="go(this.dataset.path)">${x(p)}</button>`
      :`<span class="cur">${x(p)}</span>`);
  });
  document.getElementById('bread').innerHTML=html;
}

// 全タイトル検索
function doSearch(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  if(!q){searching=false;go(curPath);return;}
  searching=true;
  document.getElementById('bread').innerHTML='<span style="color:#585b70">検索結果</span>';
  const hit=allBooks.filter(b=>b.title.toLowerCase().includes(q));
  document.getElementById('cnt').textContent=hit.length+' 冊';
  document.getElementById('msg').style.display='none';
  document.getElementById('shelf').innerHTML=hit.map(b=>
    `<div class="bc" title="${x(b.title)}">
      <img class="cv" src="/api/books/${x(b.id)}/cover" loading="lazy">
      <div class="tt">${x(b.title)}</div></div>`
  ).join('');
}

// ── 読書履歴（localStorage に各本の最後のページを保存） ──
const HKEY='ms_progress';
function loadHist(){ try{return JSON.parse(localStorage.getItem(HKEY)||'{}');}catch(e){return {};} }
function saveProg(){
  if(!rId||!rTotal) return;
  const h=loadHist();
  h[rId]={page:rPage, total:rTotal, title:rTitle, ts:Date.now()};
  try{localStorage.setItem(HKEY, JSON.stringify(h));}catch(e){}
}
function getProg(id){ return loadHist()[id]; }
function clearHistory(){
  if(confirm('読書履歴をすべて消去しますか？')){ localStorage.removeItem(HKEY); showHistory(); }
}
function delHist(id){           // 履歴の個別削除（カードの×から。rOpenとはstopPropagationで分離）
  const h=loadHist(); delete h[id];
  try{localStorage.setItem(HKEY, JSON.stringify(h));}catch(e){}
  showHistory();
}
function showHistory(){
  if(searching){document.getElementById('search').value='';searching=false;}
  if(rIsOpen) rCloseUI();
  const h=loadHist();
  const items=Object.entries(h).map(([id,v])=>({id,...v})).sort((a,b)=>(b.ts||0)-(a.ts||0));
  document.getElementById('bread').innerHTML=
    '<button data-path="" onclick="go(this.dataset.path)">⌂ 本棚</button> / <span class="cur">続き/履歴</span>'+
    (items.length?' <button onclick="clearHistory()" style="color:#f38ba8">消去</button>':'');
  document.getElementById('cnt').textContent=items.length+' 冊';
  document.getElementById('msg').style.display='none';
  if(!items.length){
    document.getElementById('shelf').innerHTML=
      '<div style="grid-column:1/-1;text-align:center;padding:60px;color:#585b70">まだ読書履歴がありません</div>';
    return;
  }
  document.getElementById('shelf').innerHTML=items.map(b=>{
    const pct=b.total?Math.round((b.page+1)/b.total*100):0;
    return `<div class="bc" title="${x(b.title)}" data-id="${x(b.id)}" data-title="${x(b.title)}" onclick="rOpen(this.dataset.id,this.dataset.title)">
      <div class="hdel" data-id="${x(b.id)}" onclick="event.stopPropagation();delHist(this.dataset.id)" title="履歴から削除">×</div>
      <img class="cv" src="/api/books/${x(b.id)}/cover" loading="lazy">
      <div class="tt">${x(b.title)}</div>
      <div class="tt" style="color:#89b4fa">${(b.page||0)+1}/${b.total||'?'}・${pct}%</div></div>`;
  }).join('');
}

// ── リーダー ────────────────────────────────────────────
let rId='', rPage=0, rTotal=0, rTitle='';
let rRtl=true, rSpread=false, rUiOn=false, rIsOpen=false;
let _skipPush=false; // popstate 経由のナビゲーション中は履歴を積まない
let rThumbTimer=null, rObs=null;
let rSiblings=[], rSiblingIdx=-1;  // 巻ナビ用: 同フォルダの本リストと現在位置

// ── スロット管理 ─────────────────────────────────────────
function rSetSlot(slotId,n){
  const slot=document.getElementById(slotId);
  const [ia,ib]=slot.querySelectorAll('img');
  if(n<0||n>=rTotal||!rId){
    ia.src='';ib.src='';ia.style.display='none';ib.style.display='';return;
  }
  ib.style.display='';
  ib.src='/api/books/'+rId+'/pages/'+n;
  if(rSpread&&n+1<rTotal){
    ia.style.display='';
    ia.src='/api/books/'+rId+'/pages/'+(n+1);
    ib.className='rp spread'; ia.className='rp spread';
    ib.style.order=rRtl?'2':'1'; ia.style.order=rRtl?'1':'2';
  } else {
    ia.src='';ia.style.display='none';
    ib.className='rp single'; ib.style.order='1';
  }
}
function rUpdateSlots(){
  // RTL: 次ページは左側から来るのでleftスロットに入れる
  // LTR: 次ページは右側から来るのでrightスロットに入れる
  rSetSlot('rsp', rRtl ? rPage+rStep() : rPage-rStep());
  rSetSlot('rsc', rPage);
  rSetSlot('rsn', rRtl ? rPage-rStep() : rPage+rStep());
}

// ── トラック操作 ──────────────────────────────────────────
function rPtSet(dx,animate){
  const t=document.getElementById('rpt');
  t.style.transition=animate?'transform 0.25s ease-out':'none';
  t.style.transform='translateX(calc(-100vw + '+dx+'px))';
}

async function rOpen(id,title){
  if(!_skipPush) history.pushState({v:'r', id, title}, '');
  rId=id; rPage=0; rTotal=0; rTitle=title;
  // 同フォルダ内での巻ナビ位置を確定
  const _si=_folderBooks.findIndex(b=>b.id===id);
  rSiblings=_si>=0?_folderBooks:[]; rSiblingIdx=_si;
  rUpdateVolNav();
  document.getElementById('rtitle-d').textContent=title;
  rIsOpen=true;
  document.getElementById('reader').style.display='flex';
  document.body.style.overflow='hidden';
  rPtSet(0,false);
  rHideUI();
  const info=await fetch('/api/books/'+id+'/info').then(r=>r.json());
  rTotal=info.count;
  document.getElementById('rslider').max=rTotal-1;
  rBuildFilm();
  // 保存された続きのページがあればそこから開く
  const pr=getProg(id);
  const start=(pr&&pr.page>0&&pr.page<rTotal)?pr.page:0;
  rLoad(start);
}

function rCloseUI(){
  // UIだけ閉じる（履歴操作なし）
  rIsOpen=false;
  document.getElementById('reader').style.display='none';
  document.body.style.overflow='';
  ['rsp','rsc','rsn'].forEach(id=>{
    document.getElementById(id).querySelectorAll('img').forEach(i=>{i.src='';});
  });
  rPtSet(0,false);
  rHideUI();
}
function rClose(){
  // ボタン・Esc からの明示的な閉じる → 履歴を戻す
  history.back();
}

function rStep(){ return rSpread?2:1; }

// 巻ナビ: dir=-1で前の巻, +1で次の巻
function rNavVol(dir){
  const idx=rSiblingIdx+dir;
  if(idx<0||idx>=rSiblings.length) return;
  rOpen(rSiblings[idx].id, rSiblings[idx].title);
}
function rUpdateVolNav(){
  const hasPrev=rSiblingIdx>0;
  const hasNext=rSiblingIdx>=0&&rSiblingIdx<rSiblings.length-1;
  const wrap=document.getElementById('rvol-nav');
  const pbtn=document.getElementById('rbtn-pvol');
  const nbtn=document.getElementById('rbtn-nvol');
  wrap.style.display=(hasPrev||hasNext)?'flex':'none';
  // 右綴じ時は下部ナビの左右を反転（左=次の巻 / 右=前の巻）
  wrap.style.flexDirection=rRtl?'row-reverse':'row';
  // 右綴じ: 次の巻=左向き / 前の巻=右向き
  pbtn.textContent=rRtl?'前の巻 ▶':'◀ 前の巻';
  nbtn.textContent=rRtl?'◀ 次の巻':'次の巻 ▶';
  pbtn.style.visibility=hasPrev?'visible':'hidden';
  nbtn.style.visibility=hasNext?'visible':'hidden';
}

function rLoad(n){
  if(rTotal===0) return;
  // 先頭より前へ → 前の巻へ確認
  if(n<0){
    if(rSiblingIdx>0){
      const prev=rSiblings[rSiblingIdx-1];
      if(confirm('「'+prev.title+'」へ移動しますか？')) rOpen(prev.id,prev.title);
    }
    return;
  }
  // 末尾より後へ → 次の巻へ確認
  if(n>=rTotal){
    if(rSiblingIdx>=0 && rSiblingIdx<rSiblings.length-1){
      const next=rSiblings[rSiblingIdx+1];
      if(confirm('「'+next.title+'」へ移動しますか？')) rOpen(next.id,next.title);
    }
    return;
  }
  n=Math.max(0,Math.min(n,rTotal-1));
  rPage=n;
  rUpdateSlots();
  const label=(n+1)+(rSpread&&n+1<rTotal?'-'+(n+2):'')+' / '+rTotal;
  document.getElementById('rslider').value=n;
  document.getElementById('rpager-d').textContent=label;
  rFilmHL(n);
  saveProg();
}

// 読む方向に応じたクリック
function rLeftClick(){  rLoad(rPage+(rRtl?+rStep():-rStep())); }
function rRightClick(){ rLoad(rPage+(rRtl?-rStep():+rStep())); }

// 切り替え
function rToggleDir(){
  rRtl=!rRtl;
  const b=document.getElementById('rbtn-dir');
  b.textContent=rRtl?'RTL ←':'LTR →';
  b.classList.toggle('on',rRtl);
  rUpdateSlots();
  rApplyRtlLayout();
  rUpdateVolNav();
  rFilmHL(rPage);
}

function rToggleSpread(){
  rSpread=!rSpread;
  const b=document.getElementById('rbtn-spr');
  b.textContent=rSpread?'見開き':'単ページ';
  b.classList.toggle('on',rSpread);
  rLoad(rPage);
}

function rApplyMode(){ rUpdateSlots(); }

// フィルム・スライダーの向き適用
function rApplyRtlLayout(){
  // dir="rtl" だけで flex items が右始まりになる。row-reverse と併用すると打ち消し合うので使わない
  const wrap=document.getElementById('rfilm-wrap');
  const slider=document.getElementById('rslider');
  wrap.dir   = rRtl ? 'rtl' : 'ltr';
  slider.dir = rRtl ? 'rtl' : 'ltr';
}

// フィルムストリップ
function rBuildFilm(){
  if(rObs){rObs.disconnect();rObs=null;}
  const film=document.getElementById('rfilm');
  film.innerHTML='';
  const wrap=document.getElementById('rfilm-wrap');
  for(let i=0;i<rTotal;i++){
    const d=document.createElement('div');
    d.className='rft'; d.dataset.page=i;
    const pg=i; d.onclick=()=>rLoad(pg);
    const img=document.createElement('img');
    img.alt=''; img.dataset.src='/api/books/'+rId+'/pages/'+i;
    d.appendChild(img); film.appendChild(d);
  }
  rApplyRtlLayout();
  rObs=new IntersectionObserver(entries=>{
    entries.forEach(e=>{
      if(e.isIntersecting){
        const img=e.target.querySelector('img');
        if(img&&img.dataset.src){img.src=img.dataset.src;delete img.dataset.src;}
      }
    });
  },{root:wrap,rootMargin:'0px 400px'});
  film.querySelectorAll('.rft').forEach(d=>rObs.observe(d));
}

function rFilmHL(n){
  const film=document.getElementById('rfilm');
  film.querySelectorAll('.rft.cur').forEach(d=>d.classList.remove('cur'));
  const cur=film.querySelector('[data-page="'+n+'"]');
  if(cur){cur.classList.add('cur');cur.scrollIntoView({inline:'center',block:'nearest',behavior:'smooth'});}
}

// UI 表示/非表示（中央クリックのみ、自動消去なし）
function rShowUI(){
  rUiOn=true;
  document.getElementById('rui-top').style.display='flex';
  document.getElementById('rui-bot').style.display='flex';
}
function rHideUI(){
  rUiOn=false;
  document.getElementById('rui-top').style.display='none';
  document.getElementById('rui-bot').style.display='none';
}
function rToggleUI(){ rUiOn?rHideUI():rShowUI(); }

// スライダー（ドラッグ中はページ番号更新、離したらジャンプ）
function rSeekInput(v){
  const n=parseInt(v);
  document.getElementById('rpager-d').textContent=(n+1)+' / '+rTotal;
  rFilmHL(n);
}
function rSeekChange(v){ rLoad(parseInt(v)); }

// マウスホイール（リーダー表示中のみ）
document.addEventListener('wheel',e=>{
  if(!rIsOpen) return;
  e.preventDefault();
  rLoad(rPage+(e.deltaY>0?+rStep():-rStep()));
},{passive:false});

// ミドルクリック → 見開き切り替え
document.addEventListener('mousedown',e=>{
  if(!rIsOpen) return;
  if(e.button===1){e.preventDefault();rToggleSpread();}
});

// ── 虫眼鏡（長押しで×2拡大レンズ・指/カーソル追従・離すと消える） ──
let rMagOn=false, rMagTimer=null, rMagJustEnded=false;
const RMAG_W=640, RMAG_H=480, RMAG_SCALE=2;   // レンズの幅/高さ(px)。本に合わせ横長
function rMagAt(cx,cy){
  const mag=document.getElementById('rmag');
  mag.style.display='none';                         // 自分を除外して直下の画像を取得
  // クリックゾーン(#rzl/#rzc/#rzr, z-index:1)が画像の上に重なるため、最前面しか返さない
  // elementFromPoint ではゾーンdivが返り画像が取れない。全要素から .rp 画像を探す。
  const el=document.elementsFromPoint(cx,cy).find(
    n=>n.tagName==='IMG'&&n.classList.contains('rp')&&n.src);
  if(!el) return;  // 画像外なら出さない
  const rect=el.getBoundingClientRect();
  const ix=cx-rect.left, iy=cy-rect.top;            // 画像内のポインタ座標
  mag.style.backgroundImage='url("'+el.src+'")';
  mag.style.backgroundSize=(rect.width*RMAG_SCALE)+'px '+(rect.height*RMAG_SCALE)+'px';
  mag.style.backgroundPosition=(RMAG_W/2-ix*RMAG_SCALE)+'px '+(RMAG_H/2-iy*RMAG_SCALE)+'px';
  mag.style.width=RMAG_W+'px'; mag.style.height=RMAG_H+'px';
  mag.style.left=(cx-RMAG_W/2)+'px'; mag.style.top=(cy-RMAG_H/2)+'px';
  mag.style.display='block';
}
function rMagStart(cx,cy){ rMagOn=true; rMagAt(cx,cy); }
function rMagStop(){
  if(rMagOn) rMagJustEnded=true;                    // 直後の click（ページ送り/UI）を1回握りつぶす
  rMagOn=false;
  if(rMagTimer){clearTimeout(rMagTimer); rMagTimer=null;}
  document.getElementById('rmag').style.display='none';
}

// ── スムーズスワイプ（カルーセル）＋ 長押し虫眼鏡 ───────────────
let rDragX=null, rDragY=null, rDragging=false;
const rdr=document.getElementById('reader');
// 虫眼鏡終了直後のクリックを無効化（capture で先取り）
rdr.addEventListener('click',e=>{
  if(rMagJustEnded){ e.stopPropagation(); e.preventDefault(); rMagJustEnded=false; }
}, true);
// マウス長押し
rdr.addEventListener('mousedown',e=>{
  if(e.button!==0||e.target.closest('#rui-top,#rui-bot')) return;
  const cx=e.clientX, cy=e.clientY;
  rMagTimer=setTimeout(()=>rMagStart(cx,cy), 320);
});
rdr.addEventListener('mousemove',e=>{
  if(rMagOn){ e.preventDefault(); rMagAt(e.clientX,e.clientY); }
  else if(rMagTimer){ clearTimeout(rMagTimer); rMagTimer=null; }
});
window.addEventListener('mouseup',()=>{ if(rMagOn||rMagTimer) rMagStop(); });
// タッチ
rdr.addEventListener('touchstart',e=>{
  if(e.target.closest('#rui-top,#rui-bot')) return;
  rDragX=e.touches[0].clientX;
  rDragY=e.touches[0].clientY;
  rDragging=false;
  rPtSet(0,false);
  const cx=rDragX, cy=rDragY;
  rMagTimer=setTimeout(()=>{ if(!rDragging) rMagStart(cx,cy); }, 380);  // 動かさず長押し
},{passive:true});
rdr.addEventListener('touchmove',e=>{
  if(rMagOn){ e.preventDefault(); rMagAt(e.touches[0].clientX,e.touches[0].clientY); return; }
  if(rDragX===null) return;
  const dx=e.touches[0].clientX-rDragX;
  const dy=e.touches[0].clientY-rDragY;
  if(!rDragging){
    if(Math.abs(dx)<8&&Math.abs(dy)<8) return;
    if(rMagTimer){clearTimeout(rMagTimer); rMagTimer=null;}   // 動いた→長押しをキャンセル
    if(Math.abs(dy)>=Math.abs(dx)){rDragX=null;return;}
    rDragging=true;
  }
  e.preventDefault();
  rPtSet(dx,false);
},{passive:false});
rdr.addEventListener('touchend',e=>{
  if(rMagTimer){clearTimeout(rMagTimer); rMagTimer=null;}
  if(rMagOn){ rMagStop(); rDragX=null; rDragging=false; return; }
  if(rDragX===null) return;
  const dx=e.changedTouches[0].clientX-rDragX;
  const wasDrag=rDragging;
  rDragX=null; rDragging=false;
  if(!wasDrag) return;
  const W=window.innerWidth;
  // RTL: 右スワイプ(dx>0)=次ページ  LTR: 左スワイプ(dx<0)=次ページ
  const advance = rRtl ? (dx>0) : (dx<0);
  const target  = rPage + (advance ? rStep() : -rStep());
  if(Math.abs(dx)>W*0.4&&target>=0&&target<rTotal){
    // advance===rRtl のとき +W(右へスライド=左スロット表示)、それ以外は -W
    rPtSet((advance===rRtl)?W:-W, true);
    document.getElementById('rpt').addEventListener('transitionend',()=>{
      rLoad(target);
      rPtSet(0,false);
    },{once:true});
  } else {
    rPtSet(0,true);
  }
},{passive:true});

// 全画面
function rToggleFS(){
  const el=document.documentElement;
  const isFs=document.fullscreenElement||document.webkitFullscreenElement;
  if(!isFs){
    const fn=el.requestFullscreen||el.webkitRequestFullscreen;
    if(fn) fn.call(el);
  } else {
    const fn=document.exitFullscreen||document.webkitExitFullscreen;
    if(fn) fn.call(document);
  }
}
document.addEventListener('fullscreenchange',()=>{
  const b=document.getElementById('rbtn-fs');
  if(b) b.textContent=document.fullscreenElement?'⛶ 縮小':'⛶ 全画面';
});
document.addEventListener('webkitfullscreenchange',()=>{
  const b=document.getElementById('rbtn-fs');
  if(b) b.textContent=document.webkitFullscreenElement?'⛶ 縮小':'⛶ 全画面';
});

// キーボード
document.addEventListener('keydown',e=>{
  if(!rIsOpen) return;
  if(e.key==='ArrowRight') rLoad(rPage+(rRtl?-rStep():+rStep()));
  else if(e.key==='ArrowLeft')  rLoad(rPage+(rRtl?+rStep():-rStep()));
  else if(e.key==='ArrowDown')  rLoad(rPage+rStep());
  else if(e.key==='ArrowUp')    rLoad(rPage-rStep());
  else if(e.key==='Escape') rClose();
});

// 戻るボタン対応
window.addEventListener('popstate', e=>{
  const st=e.state;
  _skipPush=true;
  if(rIsOpen) rCloseUI();
  if(!st||st.v==='f') go(st?.path??'');
  else if(st.v==='r') rOpen(st.id, st.title, st.rel||'');
  setTimeout(()=>{_skipPush=false;},0);
});

async function init(){
  dbg('JS起動');
  try{
    // 初回は replaceState（初期状態として記録。pushState しない）
    history.replaceState({v:'f', path:''}, '');
    _skipPush=true;
    await go('');
    _skipPush=false;
    fetch('/api/books').then(r=>r.json()).then(d=>{allBooks=d;});
  }catch(e){document.getElementById('msg').textContent='初期化エラー: '+e;}
}
init();
</script>
</body>
</html>"""
    resp = HTMLResponse(_html, headers={"Cache-Control": "no-store"})
    # 「ブラウザで開く」など ?token= 付きで開かれたら、トークンをCookieに保存する。
    # <img src="/api/..."> はリクエストヘッダを付けられないので、Cookieで認証を通す。
    qtoken = request.query_params.get("token")
    if qtoken:
        resp.set_cookie("ms_token", qtoken, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 365)
    return resp

@api.post("/api/devices/register")
async def register_device(request: Request):
    """端末登録（認証不要）。LANペアリング時に発行したノンスで正当性を確認する。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    reg_nonce   = str(body.get("reg_nonce",   ""))
    device_id   = str(body.get("device_id",   ""))[:64]
    device_name = str(body.get("device_name", "Android"))[:50]
    if not device_id:
        raise HTTPException(400, "device_id required")
    # ノンス検証（メモリ内・使い捨て）
    if reg_nonce not in _reg_nonces or time.time() > _reg_nonces[reg_nonce]:
        _reg_nonces.pop(reg_nonce, None)
        raise HTTPException(403, "Invalid or expired registration nonce")
    del _reg_nonces[reg_nonce]
    devices = _config.setdefault("devices", {})
    # 既承認端末の再ペアリング（トークンは変えない）
    if device_id in devices and devices[device_id].get("status") == "approved":
        _log_queue.put(f"[認証] 既承認端末が再ペアリング: {device_name}")
        return {"status": "already_approved"}
    reg_token = secrets.token_urlsafe(32)
    devices[device_id] = {
        "name":         device_name,
        "status":       "pending",
        "reg_token":    reg_token,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_config(_config)
    _log_queue.put(
        f"[認証] 新端末が接続要求中: {device_name} ({device_id[:8]}) "
        f"— GUIの「端末管理」ボタンで承認してください"
    )
    return {"status": "pending", "reg_token": reg_token}


@api.get("/api/devices/status")
async def device_status(request: Request):
    """端末承認状態をポーリング。承認済みになったらトークンを返す。
    ブルートフォース対策: reg_token不一致(404)が続いたIPは一時ブロックする
    （正規端末は自分のreg_tokenが常に一致するため、ブロック中でも404にはならず通る）。
    """
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


@api.get("/api/status")
def status(_: str = Depends(_check_auth)):
    return {
        "books": len(_books),
        "unrar": UNRAR_AVAILABLE,
        "version": "1.0",
    }

@api.get("/api/books")
def list_books(_: str = Depends(_check_auth)):
    """本棚一覧（相対パス付き）。Android の書棚画面・全文検索で使う。"""
    return [
        {"id": bid, "title": info["title"], "rel": info.get("rel", ".")}
        for bid, info in _books.items()
    ]

@api.get("/api/folders")
def list_folders(path: str = "", _: str = Depends(_check_auth)):
    """
    指定パス直下のサブフォルダ一覧と直置き本を返す。
    path="" → ルート, path="ジャンル1" → その直下
    """
    subfolders: dict[str, dict] = {}
    direct_books: list[dict]    = []

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
    """ページ数など詳細情報。初回アクセス時にページ一覧をキャッシュする。"""
    info = _books.get(bid)
    if not info:
        raise HTTPException(404, "Book not found")
    if "pages" not in info:
        info["pages"] = get_page_list(info["path"])
    start_warm(bid)   # 本を開いた合図 → 全ページをバックグラウンドで温め始める
    return {"id": bid, "title": info["title"], "count": len(info["pages"])}

@api.get("/api/connection-info")
def api_connection_info(_: str = Depends(_check_auth)):
    """外部接続用の接続情報（LAN内でアプリが受け取り保存する。Phase 2でroomId/トークンも追加予定）。"""
    return _connection_info()

@api.get("/api/books/{bid}/cover")
def book_cover(bid: str, _: str = Depends(_check_auth)):
    """表紙サムネイル JPEG を返す。失敗時はグレー画像を返す（500は出さない）。"""
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
        # 同じエラーは1回だけログに出す
        key = f"{type(e).__name__}:{str(e)[:80]}"
        if key not in _cover_errors:
            _cover_errors.add(key)
            _log_queue.put(f"[WARN] 表紙取得失敗 ({info.get('title','')[:30]}): {e}")
        return Response(_placeholder_jpeg(), media_type="image/jpeg")

@api.get("/api/books/{bid}/pages/{n}")
def get_page(bid: str, n: int, _: str = Depends(_check_auth)):
    """n ページ目の画像 JPEG を返す（スマホ向けにリサイズ済み・ディスクキャッシュ付き）。"""
    info = _books.get(bid)
    if not info:
        raise HTTPException(404, "Book not found")
    if "pages" not in info:
        info["pages"] = get_page_list(info["path"])
    if n < 0 or n >= len(info["pages"]):
        raise HTTPException(404, f"Page {n} out of range (0-{len(info['pages'])-1})")

    # ── キャッシュ命中: アーカイブを開かず・リサイズせずディスクから即返す ──
    cache_path = _page_cache_path(bid, n)
    if cache_path.exists():
        try:
            return Response(cache_path.read_bytes(), media_type="image/jpeg")
        except OSError:
            pass  # 壊れていれば下で作り直す

    # ── キャッシュ未命中: 生成 → 保存 → 配信 ──
    try:
        data = read_raw_image(info["path"], info["pages"][n])
        jpeg = resize_jpeg(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

    _page_cache_put(cache_path, jpeg)   # 保存失敗時も内部で握りつぶし、配信は成功させる
    return Response(jpeg, media_type="image/jpeg")

@api.post("/api/scan")
def api_scan(_: str = Depends(_check_auth)):
    """本棚を再スキャンする。"""
    n = scan_books(_config.get("scan_dirs", []))
    return {"books": n}

# ─── WebRTC P2P シグナリング ──────────────────────────────────────────────────
# Firebase Realtime DB + aiortc でP2P接続を確立し、データチャネルでAPIをトンネル化する。
# Firebase未設定 / aiortcが未インストールの場合は起動しない（LAN/VPN動作に影響なし）。
# 必要パッケージ: pip install aiortc
import struct
import urllib.request
import urllib.error

_signaling_running: bool = False
_active_peers:  dict     = {}   # session_id → RTCPeerConnection


def _fb_cfg() -> dict | None:
    """有効なFirebase設定を返す。config.json側の値があれば優先、無ければ
    開発者既定(DEFAULT_FIREBASE)を使う。どちらも未設定なら None（=P2P無効）。"""
    user_fb = _config.get("firebase", {}) or {}
    api_key      = user_fb.get("api_key")      or DEFAULT_FIREBASE.get("api_key", "")
    database_url = user_fb.get("database_url") or DEFAULT_FIREBASE.get("database_url", "")
    if not api_key or not database_url:
        return None
    return {"api_key": api_key, "database_url": database_url}


class _FbAuthError(Exception):
    """Firebaseが401を返した（idTokenの期限切れ等）。呼び出し側で再認証する。"""
    pass


def _http_json(url: str, method: str = "GET",
               data: dict | None = None, token: str = "") -> dict | None:
    """Firebase REST API の同期呼び出し。urllib 標準ライブラリのみ使用。
    401（認証切れ）は _FbAuthError を送出し、呼び出し側で再認証＆リトライさせる。"""
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}auth={token}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    body = json.dumps(data).encode("utf-8") if data is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=10) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text.strip() not in ("", "null") else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _FbAuthError(url)
        _log_queue.put(f"[WebRTC] Firebase HTTP {e.code}: {url}")
    except Exception as e:
        _log_queue.put(f"[WebRTC] 通信エラー: {e}")
    return None


def _fb_get(db_url: str, path: str, token: str) -> dict | None:
    return _http_json(f"{db_url}/{path}.json", token=token)


def _fb_put(db_url: str, path: str, data, token: str) -> None:
    _http_json(f"{db_url}/{path}.json", method="PUT", data=data, token=token)


def _firebase_anon_signin(api_key: str) -> str | None:
    """Firebase匿名認証でidTokenを取得する。"""
    try:
        result = _http_json(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}",
            method="POST",
            data={"returnSecureToken": True},
        )
    except _FbAuthError:
        _log_queue.put("[WebRTC] 匿名認証が401。api_key/匿名認証の有効化を確認してください")
        return None
    return result.get("idToken") if result else None


def _reauth(auth: dict) -> bool:
    """auth['token'] を新しいidTokenに更新する。成功でTrue。auth={'api_key','token'}。"""
    tok = _firebase_anon_signin(auth["api_key"])
    if tok:
        auth["token"] = tok
        _log_queue.put("[WebRTC] トークンを再認証しました")
        return True
    return False


def _fb_get_retry(db_url: str, path: str, auth: dict) -> dict | None:
    """_fb_get の401耐性版。期限切れなら再認証して1回だけリトライ。"""
    try:
        return _fb_get(db_url, path, auth["token"])
    except _FbAuthError:
        if not _reauth(auth):
            return None
        try:
            return _fb_get(db_url, path, auth["token"])
        except _FbAuthError:
            return None


def _fb_put_retry(db_url: str, path: str, data, auth: dict) -> None:
    """_fb_put の401耐性版。期限切れなら再認証して1回だけリトライ。"""
    try:
        _fb_put(db_url, path, data, auth["token"])
        return
    except _FbAuthError:
        if not _reauth(auth):
            return
        try:
            _fb_put(db_url, path, data, auth["token"])
        except _FbAuthError:
            pass


def _fb_delete(db_url: str, path: str, token: str) -> None:
    # Firebase でノード削除は HTTP DELETE。PUT に本文 None を渡すと400になるため専用にする。
    _http_json(f"{db_url}/{path}.json", method="DELETE", token=token)


def _fb_delete_retry(db_url: str, path: str, auth: dict) -> None:
    """_fb_delete の401耐性版。期限切れなら再認証して1回だけリトライ。"""
    try:
        _fb_delete(db_url, path, auth["token"])
        return
    except _FbAuthError:
        if not _reauth(auth):
            return
        try:
            _fb_delete(db_url, path, auth["token"])
        except _FbAuthError:
            pass


# Firebase push ID の文字セット（先頭8文字に生成時刻msが48bitで埋まっている）
_PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"


def _push_id_time_ms(sid: str) -> int | None:
    """Firebase push ID 先頭8文字から生成時刻(ms)を復元する。解析不能なら None。"""
    try:
        t = 0
        for c in sid[:8]:
            t = t * 64 + _PUSH_CHARS.index(c)
        return t
    except ValueError:
        return None


def _offer_origin(offer_sdp: str) -> str:
    """offer SDP の ICE candidate から接続元を要約（自分のLAN端末か外部かの判別材料）。"""
    pub, lan, mdns = set(), set(), False
    for m in re.finditer(r"candidate:\S+ \d+ \S+ \d+ (\S+) \d+ typ (\w+)", offer_sdp):
        addr = m.group(1)
        if addr.endswith(".local"):       # 近年のWebRTCは host候補をmDNS名で秘匿する
            mdns = True
            continue
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        # プライベート/リンクローカル/ループバックはLAN、それ以外は外部扱い
        # （携帯回線のCGNAT等で is_global が False になる住所も外部として拾うため）
        if ip.is_private or ip.is_link_local or ip.is_loopback:
            lan.add(addr)
        else:
            pub.add(addr)
    parts = []
    if pub:
        parts.append("外部 " + ",".join(sorted(pub)))
    if lan:
        parts.append("LAN " + ",".join(sorted(lan)))
    if mdns and not parts:
        parts.append("LAN(mDNS秘匿)")
    return " / ".join(parts) if parts else "不明"


def _dc_response(req_id: int, status: int, content_type: str, body: bytes) -> bytes:
    """データチャネルレスポンス: [4B header_len][header JSON][body]"""
    header = json.dumps(
        {"id": req_id, "status": status, "content_type": content_type}
    ).encode("utf-8")
    return struct.pack(">I", len(header)) + header + body


def _device_label_for_message(message: bytes | str) -> str:
    """DCリクエストのtokenから端末を逆引きし、接続元ログ用のラベルを返す。"""
    try:
        req = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
        supplied = str(req.get("token", ""))
    except Exception:
        return "解析失敗"
    found = _find_device_by_token(supplied)
    if found:
        did, d = found
        return f"端末「{d.get('name', '?')}」({did[:8]}) として認証成功"
    return "不明なトークンで認証失敗"


def _handle_dc_request(message: bytes | str) -> bytes:
    """JSON形式のリクエストを処理してバイナリレスポンスを返す（同期・ブロッキングOK）。"""
    try:
        req = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
        req_id   = int(req.get("id", 0))
        req_type = req.get("type", "")
        bid      = req.get("bid", "")
        n        = int(req.get("n", 0))
        path     = req.get("path", "")
        supplied = str(req.get("token", ""))
    except Exception as e:
        return _dc_response(0, 400, "application/json",
                            json.dumps({"error": str(e)}).encode())

    # 認証層2: room_idでP2Pが成立しても、承認済み端末トークンが無ければ本を配らない。
    if not _find_device_by_token(supplied):
        return _dc_response(req_id, 401, "application/json",
                            json.dumps({"error": "unauthorized"}).encode())
    try:
        if req_type == "status":
            return _dc_response(req_id, 200, "application/json",
                                json.dumps({"service": "comicserver", "version": "2.0"}).encode())

        if req_type == "connection_info":
            return _dc_response(req_id, 200, "application/json",
                                json.dumps(_connection_info()).encode())

        if req_type == "folders":
            subfolders: dict = {}
            direct_books: list = []
            for bid2, info in _books.items():
                rel = info.get("rel", ".")
                if path == "":
                    if rel == ".":
                        direct_books.append({"id": bid2, "title": info["title"]})
                    else:
                        top = rel.split("/")[0]
                        if top not in subfolders:
                            subfolders[top] = {"name": top, "path": top, "count": 0, "ids": []}
                        subfolders[top]["count"] += 1
                        if len(subfolders[top]["ids"]) < 4:
                            subfolders[top]["ids"].append(bid2)
                else:
                    if rel == path:
                        direct_books.append({"id": bid2, "title": info["title"]})
                    elif rel.startswith(path + "/"):
                        rest = rel[len(path) + 1:]
                        sub  = rest.split("/")[0]
                        full = path + "/" + sub
                        if sub not in subfolders:
                            subfolders[sub] = {"name": sub, "path": full, "count": 0, "ids": []}
                        subfolders[sub]["count"] += 1
                        if len(subfolders[sub]["ids"]) < 4:
                            subfolders[sub]["ids"].append(bid2)
            result = {
                "path":    path,
                "folders": sorted(subfolders.values(), key=lambda x: natural_key(x["name"])),
                "books":   sorted(direct_books,        key=lambda b: natural_key(b["title"])),
            }
            return _dc_response(req_id, 200, "application/json",
                                json.dumps(result).encode())

        if req_type == "books":
            items = [{"id": k, "title": v["title"], "rel": v.get("rel", "")}
                     for k, v in sorted(_books.items(),
                         key=lambda x: natural_key(x[1]["title"]))]
            return _dc_response(req_id, 200, "application/json",
                                json.dumps(items).encode())

        if req_type == "book_info":
            info = _books.get(bid)
            if not info:
                return _dc_response(req_id, 404, "application/json", b'"not found"')
            if "pages" not in info:
                info["pages"] = get_page_list(info["path"])
            return _dc_response(req_id, 200, "application/json",
                                json.dumps({"id": bid, "title": info["title"],
                                            "count": len(info["pages"])}).encode())

        if req_type == "cover":
            data = _cover_get(bid)
            if data:
                return _dc_response(req_id, 200, "image/jpeg", data)
            info = _books.get(bid)
            if not info:
                return _dc_response(req_id, 404, "application/json", b'"not found"')
            if "pages" not in info:
                info["pages"] = get_page_list(info["path"])
            if not info["pages"]:
                return _dc_response(req_id, 200, "image/jpeg", _placeholder_jpeg())
            raw = read_raw_image(info["path"], info["pages"][0])
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((COVER_W, COVER_H), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            cover = buf.getvalue()
            _cover_put(bid, cover)
            return _dc_response(req_id, 200, "image/jpeg", cover)

        if req_type == "page":
            info = _books.get(bid)
            if not info:
                return _dc_response(req_id, 404, "application/json", b'"not found"')
            if "pages" not in info:
                info["pages"] = get_page_list(info["path"])
            pages = info["pages"]
            if n < 0 or n >= len(pages):
                return _dc_response(req_id, 404, "application/json",
                                    json.dumps({"error": f"page {n} out of range"}).encode())
            cache_path = _page_cache_path(bid, n)
            if cache_path.exists():
                try:
                    return _dc_response(req_id, 200, "image/jpeg", cache_path.read_bytes())
                except OSError:
                    pass
            raw  = read_raw_image(info["path"], pages[n])
            jpeg = resize_jpeg(raw)
            _page_cache_put(cache_path, jpeg)
            return _dc_response(req_id, 200, "image/jpeg", jpeg)

        return _dc_response(req_id, 400, "application/json",
                            json.dumps({"error": f"unknown type: {req_type}"}).encode())
    except Exception as e:
        return _dc_response(req_id, 500, "application/json",
                            json.dumps({"error": str(e)}).encode())


async def _run_peer_async(session_id: str, offer_sdp: str,
                          db_url: str, room_id: str, auth: dict,
                          loop: asyncio.AbstractEventLoop) -> None:
    """1セッション分のWebRTC接続を確立してデータチャネルを処理する。"""
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer  # type: ignore

    stun = _config.get("stun_servers", ["stun:stun.l.google.com:19302"])
    ice_servers = [RTCIceServer(urls=stun)]
    turn = _config.get("turn", {})
    if turn.get("url"):
        ice_servers.append(RTCIceServer(
            urls=[turn["url"]],
            username=turn.get("username", ""),
            credential=turn.get("credential", ""),
        ))
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    _active_peers[session_id] = pc

    try:
        # offer SDPにICE候補が埋め込み済み（アプリ側が非トリクルで送る）
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer"))

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # ICE収集完了を最大10秒待つ
        for _ in range(100):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        # AnswerをFirebase に書く（ICEはSDP埋め込み済みなので追記不要）
        await loop.run_in_executor(None, lambda: _fb_put_retry(
            db_url, f"rooms/{room_id}/sessions/{session_id}/answer",
            {"sdp": pc.localDescription.sdp, "type": "answer"}, auth,
        ))
        _log_queue.put(f"[WebRTC] Answer送信完了: {session_id[:8]}")

        # データチャネルを待つ
        dc_ready = asyncio.Event()

        @pc.on("datachannel")
        def on_datachannel(channel):
            _log_queue.put(f"[WebRTC] P2P接続確立: {session_id[:8]}")
            dc_ready.set()
            device_logged = False

            @channel.on("message")
            def on_message(message):
                async def _process():
                    nonlocal device_logged
                    try:
                        resp = await loop.run_in_executor(None, _handle_dc_request, message)
                        channel.send(resp)
                    except Exception as e:
                        _log_queue.put(f"[WebRTC] DCエラー: {e}")
                        return
                    if not device_logged:
                        device_logged = True
                        _log_queue.put(
                            f"[WebRTC] {session_id[:8]}: {_device_label_for_message(message)}"
                        )
                asyncio.create_task(_process())

        try:
            await asyncio.wait_for(dc_ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            _log_queue.put(f"[WebRTC] タイムアウト: {session_id[:8]}")
            return

        # 接続が切れるまで維持
        while pc.connectionState not in ("failed", "closed", "disconnected"):
            await asyncio.sleep(5)

    except Exception as e:
        _log_queue.put(f"[WebRTC] ピアエラー ({session_id[:8]}): {e}")
    finally:
        await pc.close()
        _active_peers.pop(session_id, None)
        try:
            await loop.run_in_executor(None, lambda: _fb_delete_retry(
                db_url, f"rooms/{room_id}/sessions/{session_id}", auth,
            ))
        except Exception:
            pass


async def _signaling_loop_async(api_key: str, db_url: str, room_id: str) -> None:
    """Firebase をポーリングして新セッションが来たら _run_peer_async をタスク起動する。"""
    loop = asyncio.get_event_loop()

    token = await loop.run_in_executor(None, lambda: _firebase_anon_signin(api_key))
    if not token:
        _log_queue.put("[WebRTC] Firebase認証失敗")
        return
    # 全Firebase呼び出しで共有する認証状態。401検知時に token が更新され全員に反映される
    auth = {"api_key": api_key, "token": token}

    await loop.run_in_executor(None, lambda: _fb_put_retry(
        db_url, f"rooms/{room_id}/presence", {"host": True}, auth,
    ))
    _log_queue.put("[WebRTC] Firebase接続完了。セッション待機中...")

    known: set[str] = set()
    # 実時間ベースで期限前に先回り更新（PCスリープ後もズレない。万一切れても下のretryが拾う）
    next_refresh = time.time() + 3300   # 有効期限1時間の5分前

    while _signaling_running:
        try:
            if time.time() >= next_refresh:
                ok = await loop.run_in_executor(None, lambda: _reauth(auth))
                next_refresh = time.time() + (3300 if ok else 60)  # 失敗時は1分後に再試行

            sessions = (await loop.run_in_executor(
                None, lambda: _fb_get_retry(db_url, f"rooms/{room_id}/sessions", auth),
            )) or {}

            for sid, sdata in sessions.items():
                if sid in known:
                    continue
                if (isinstance(sdata, dict)
                        and sdata.get("offer")
                        and not sdata.get("answer")):
                    known.add(sid)
                    offer_sdp = sdata["offer"].get("sdp", "")
                    # サーバー停止中にアプリが書いた古いoffer(ゾンビ)は応答せず破棄する。
                    # Answerしても相手は居らず30秒タイムアウトするだけなのでログも無駄に流れる。
                    ts = _push_id_time_ms(sid)
                    if ts is not None and time.time() * 1000 - ts > 90_000:
                        _log_queue.put(f"[WebRTC] 古いセッションを破棄: {sid[:8]}")
                        await loop.run_in_executor(None, lambda s=sid: _fb_delete_retry(
                            db_url, f"rooms/{room_id}/sessions/{s}", auth))
                        continue
                    _log_queue.put(
                        f"[WebRTC] 新セッション: {sid[:8]}  接続元: {_offer_origin(offer_sdp)}")
                    asyncio.create_task(
                        _run_peer_async(sid, offer_sdp, db_url, room_id, auth, loop)
                    )
        except Exception as e:
            _log_queue.put(f"[WebRTC] シグナリングエラー: {e}")

        await asyncio.sleep(3)


def _signaling_thread_main() -> None:
    """シグナリングスレッドのエントリーポイント。"""
    global _signaling_running

    fb = _fb_cfg()
    if not fb:
        _log_queue.put("[WebRTC] Firebase未設定のためP2Pシグナリングを無効化します")
        _signaling_running = False
        return

    try:
        import aiortc  # noqa: F401 — インストール確認
    except ImportError:
        _log_queue.put("[WebRTC] aiortcがインストールされていません。"
                       "pip install aiortc を実行してください。")
        _signaling_running = False
        return

    api_key = fb["api_key"]
    db_url  = fb["database_url"].rstrip("/")
    room_id = _config.get("room_id", "")
    if not room_id:
        _log_queue.put("[WebRTC] room_id未設定")
        _signaling_running = False
        return

    _log_queue.put(f"[WebRTC] シグナリング開始 (room: {room_id[:8]}…)")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_signaling_loop_async(api_key, db_url, room_id))
    except Exception as e:
        _log_queue.put(f"[WebRTC] シグナリング終了: {e}")
    finally:
        _signaling_running = False
        loop.close()


def start_webrtc_signaling() -> None:
    """WebRTCシグナリングスレッドを起動（多重起動防止）。"""
    global _signaling_running
    if _signaling_running:
        return
    _signaling_running = True
    threading.Thread(target=_signaling_thread_main, daemon=True).start()

# ─── Uvicorn スレッド ──────────────────────────────────────────────────────────
class _NoSignalServer(uvicorn.Server):
    """スレッド内で動かすため、シグナルハンドラ登録をスキップする"""
    def install_signal_handlers(self) -> None:
        pass

class _UvicornThread(threading.Thread):
    def __init__(self, host: str, port: int,
                 ssl_certfile: str = "", ssl_keyfile: str = ""):
        super().__init__(daemon=True)
        kw: dict = dict(host=host, port=port, log_config=None)
        if ssl_certfile and ssl_keyfile:
            kw["ssl_certfile"] = ssl_certfile
            kw["ssl_keyfile"]  = ssl_keyfile
        self._srv = _NoSignalServer(uvicorn.Config(api, **kw))
        self.error: str = ""

    def run(self) -> None:
        import traceback
        try:
            asyncio.run(self._srv.serve())
        except Exception as e:
            tb = traceback.format_exc()
            self.error = str(e)
            _log_queue.put(f"[ERROR] サーバー起動失敗: {e}")
            _log_queue.put(f"[ERROR] {tb}")

    def stop(self) -> None:
        self._srv.should_exit = True

_server_thread:    _UvicornThread | None = None
_server_thread_v6: _UvicornThread | None = None

# uvicorn のログを _log_queue に流す
class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access":
            msg = record.getMessage()
            if '" 200' in msg or '" 304' in msg or 'favicon.ico' in msg:
                return  # 正常アクセス・faviconは表示しない
        _log_queue.put(f"[{record.levelname}] {record.getMessage()}")

_q_handler = _QueueHandler()
_q_handler.setLevel(logging.INFO)
# 親の "uvicorn" だけにハンドラを付ける。子は propagate で親に流れるので重複しない
_uv_root = logging.getLogger("uvicorn")
_uv_root.addHandler(_q_handler)
_uv_root.setLevel(logging.INFO)
# 子ロガーは親に任せる（propagate=True はデフォルトなので設定不要）
for _n in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_n).setLevel(logging.INFO)

# ─── カラーパレット ────────────────────────────────────────────────────────────
BG       = "#1e1e2e"
PANEL    = "#181825"
FG       = "#cdd6f4"
FG_DIM   = "#a6adc8"
FG_GREEN = "#a6e3a1"
FG_RED   = "#f38ba8"
ACCENT   = "#89b4fa"

# ─── GUI アプリ ────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MangaServer")
        self.geometry("720x560")
        self.minsize(620, 480)
        self.configure(bg=BG)
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass
        self._tray = None
        self.protocol("WM_DELETE_WINDOW", self._on_close_window)

        global _config
        _config = load_config()

        self._build()
        self._update_dir_list()
        self._log(f"MangaServer 起動 | Python {sys.version.split()[0]}")
        if not UNRAR_AVAILABLE:
            self._log(f"[警告] WinRAR が見つかりません（RAR/CBR は使用不可）: {UNRAR_PATH}")

    # ── UI 構築 ────────────────────────────────────────────────────────────────
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._build_header()
        self._build_main()
        self._build_log()
        self._build_toolbar()
        self.after(100, self._poll_log)

    def _build_header(self):
        f = tk.Frame(self, bg=PANEL, pady=8)
        f.grid(row=0, column=0, sticky="ew")
        f.columnconfigure(2, weight=1)

        tk.Label(f, text="MangaServer", bg=PANEL, fg=FG,
                 font=("Yu Gothic UI", 13, "bold")).grid(row=0, column=0, padx=14)

        self._dot = tk.Label(f, text="●", bg=PANEL, fg=FG_RED, font=("", 16))
        self._dot.grid(row=0, column=1, padx=(0, 4))

        self._status_lbl = tk.Label(f, text="停止中", bg=PANEL, fg=FG_DIM,
                                     font=("Yu Gothic UI", 10))
        self._status_lbl.grid(row=0, column=2, sticky="w")

    def _build_main(self):
        main = tk.Frame(self, bg=BG)
        main.grid(row=1, column=0, sticky="ew", padx=10, pady=(10, 0))
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)

        # 左: スキャンフォルダ
        lf = tk.LabelFrame(main, text=" スキャンフォルダ ", bg=BG, fg=FG,
                            font=("Yu Gothic UI", 9), pady=4)
        lf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        self._dir_lb = tk.Listbox(
            lf, bg=PANEL, fg=FG, selectbackground=ACCENT,
            selectforeground=BG, font=("Consolas", 9),
            height=5, borderwidth=0, highlightthickness=0)
        self._dir_lb.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=2)

        btn_f = tk.Frame(lf, bg=BG)
        btn_f.grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
        tk.Button(btn_f, text="＋ 追加", bg="#2a2a4a", fg=FG, relief="flat",
                  font=("Yu Gothic UI", 9), padx=8,
                  command=self._add_dir).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_f, text="－ 削除", bg="#2a2a4a", fg=FG, relief="flat",
                  font=("Yu Gothic UI", 9), padx=8,
                  command=self._remove_dir).pack(side=tk.LEFT, padx=2)

        self._books_lbl = tk.Label(lf, text="登録: 0 冊", bg=BG, fg=FG_DIM,
                                    font=("Yu Gothic UI", 8))
        self._books_lbl.grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        # 右: 設定
        rf = tk.LabelFrame(main, text=" 接続設定 ", bg=BG, fg=FG,
                           font=("Yu Gothic UI", 9), pady=4)
        rf.grid(row=0, column=1, sticky="nsew")
        rf.columnconfigure(1, weight=1)

        # ポート番号
        tk.Label(rf, text="ポート番号", bg=BG, fg=FG_DIM,
                 font=("Yu Gothic UI", 9)).grid(row=0, column=0, padx=8, pady=5, sticky="w")
        self._port_entry = tk.Entry(rf, bg=PANEL, fg=FG, insertbackground=FG,
                                    relief="flat", font=("Consolas", 10))
        self._port_entry.insert(0, str(_config.get("port", 8765)))
        self._port_entry.grid(row=0, column=1, padx=(0, 8), pady=5, sticky="ew")

        tk.Label(rf, text="※ アプリは同じWi-Fiで「LAN内を探す」ボタンを押すと自動でペアリングできます",
                 bg=BG, fg=FG_DIM, font=("Yu Gothic UI", 8),
                 wraplength=240, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 0))

        tk.Button(rf, text="設定を保存", bg="#2a2a4a", fg=FG, relief="flat",
                  font=("Yu Gothic UI", 9), padx=8,
                  command=self._save_settings).grid(
            row=2, column=0, columnspan=2, pady=(8, 4))

        tk.Button(rf, text="端末管理...", bg="#1a2a3a", fg=ACCENT, relief="flat",
                  font=("Yu Gothic UI", 9), padx=8,
                  command=self._manage_devices).grid(
            row=3, column=0, columnspan=2, pady=(0, 4))

    def _build_log(self):
        f = tk.Frame(self, bg=BG)
        f.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)

        self._log_box = tk.Text(
            f, bg="#0d0d1a", fg="#d4d4d4",
            font=("Consolas", 8), state=tk.DISABLED,
            borderwidth=0, highlightthickness=0, height=10)
        vsb = ttk.Scrollbar(f, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=vsb.set)
        self._log_box.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

    def _build_toolbar(self):
        f = tk.Frame(self, bg=BG)
        f.grid(row=3, column=0, pady=8)

        self._start_btn = tk.Button(
            f, text="▶  サーバー起動", width=16,
            bg="#1a472a", fg=FG_GREEN, activebackground="#155724",
            font=("Yu Gothic UI", 10, "bold"), relief="flat", pady=6,
            command=self._start_server)
        self._start_btn.pack(side=tk.LEFT, padx=8)

        self._stop_btn = tk.Button(
            f, text="■  停止", width=10,
            bg="#3a0a0a", fg=FG_RED, activebackground="#2d0808",
            font=("Yu Gothic UI", 10), relief="flat", pady=6,
            state=tk.DISABLED, command=self._stop_server)
        self._stop_btn.pack(side=tk.LEFT, padx=4)

        tk.Button(
            f, text="再スキャン", width=10,
            bg="#2a2a4a", fg=FG, relief="flat",
            font=("Yu Gothic UI", 10), pady=6,
            command=self._do_scan).pack(side=tk.LEFT, padx=8)

        self._browser_btn = tk.Button(
            f, text="🌐 ブラウザで開く", width=15,
            bg="#2a2a4a", fg=ACCENT, relief="flat",
            font=("Yu Gothic UI", 10), pady=6,
            state=tk.DISABLED, command=self._open_browser)
        self._browser_btn.pack(side=tk.LEFT, padx=4)

        tk.Button(
            f, text="—  最小化", width=10,
            bg="#2a2a4a", fg=FG_DIM, relief="flat",
            font=("Yu Gothic UI", 10), pady=6,
            command=self._minimize_action).pack(side=tk.LEFT, padx=4)

    # ── フォルダ操作 ───────────────────────────────────────────────────────────
    def _add_dir(self):
        d = filedialog.askdirectory(title="スキャンするフォルダを選択")
        if d and d not in _config["scan_dirs"]:
            _config["scan_dirs"].append(d)
            save_config(_config)
            self._update_dir_list()
            self._log(f"フォルダ追加: {d}")
            # 稼働中サーバーの本棚(_books)へ即反映（再起動不要）
            threading.Thread(target=self._scan_bg, daemon=True).start()

    def _remove_dir(self):
        sel = self._dir_lb.curselection()
        if not sel:
            return
        d = _config["scan_dirs"][sel[0]]
        _config["scan_dirs"].pop(sel[0])
        save_config(_config)
        self._update_dir_list()
        self._log(f"フォルダ削除: {d}")
        # 稼働中サーバーの本棚(_books)へ即反映（再起動不要）
        threading.Thread(target=self._scan_bg, daemon=True).start()

    def _update_dir_list(self):
        self._dir_lb.delete(0, tk.END)
        for d in _config.get("scan_dirs", []):
            self._dir_lb.insert(tk.END, d)

    # ── 設定保存 ───────────────────────────────────────────────────────────────
    def _save_settings(self):
        try:
            _config["port"] = int(self._port_entry.get().strip())
        except ValueError:
            messagebox.showerror("エラー", "ポート番号は整数で入力してください")
            return
        save_config(_config)
        self._log("設定を保存しました")

    # ── 端末管理 ─────────────────────────────────────────────────────────────────
    def _manage_devices(self):
        """端末管理ダイアログを開く（承認・抹消）。"""
        dlg = tk.Toplevel(self)
        dlg.title("接続端末の管理")
        dlg.configure(bg=BG)
        dlg.geometry("540x310")
        dlg.resizable(True, False)
        dlg.grab_set()

        tk.Label(dlg, text="接続端末の管理", bg=BG, fg=FG,
                 font=("Yu Gothic UI", 11, "bold")).pack(pady=(10, 2))
        tk.Label(dlg, text="承認待ち端末を選択して「承認」を押してください",
                 bg=BG, fg=FG_DIM, font=("Yu Gothic UI", 8)).pack()

        content = tk.Frame(dlg, bg=BG)
        content.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        lb_frame = tk.Frame(content, bg=BG)
        lb_frame.pack(fill=tk.BOTH, expand=True)
        lb = tk.Listbox(lb_frame, bg=PANEL, fg=FG, selectbackground=ACCENT,
                        selectforeground=BG, font=("Yu Gothic UI", 9),
                        height=8, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(lb_frame, command=lb.yview)
        lb.configure(yscrollcommand=vsb.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame = tk.Frame(content, bg=BG)
        btn_frame.pack(pady=6, anchor="w")
        approve_btn = tk.Button(btn_frame, text="✓ 承認", bg="#1a472a", fg=FG_GREEN,
                                relief="flat", font=("Yu Gothic UI", 9), padx=8,
                                state=tk.DISABLED)
        approve_btn.pack(side=tk.LEFT, padx=4)
        revoke_btn = tk.Button(btn_frame, text="✕ 抹消", bg="#3a0a0a", fg=FG_RED,
                               relief="flat", font=("Yu Gothic UI", 9), padx=8,
                               state=tk.DISABLED)
        revoke_btn.pack(side=tk.LEFT, padx=4)

        dev_ids: list[str | None] = []

        def refresh():
            nonlocal dev_ids
            try:
                sel = lb.curselection()
                lb.delete(0, tk.END)
                dev_ids = []
                devices = _config.get("devices", {})
                for did, d in devices.items():
                    if did == "browser-local":
                        continue
                    st   = d.get("status", "pending")
                    name = d.get("name", "不明")
                    ts   = (d.get("approved_at") or d.get("requested_at") or "")[:10]
                    icon = "🟡" if st == "pending" else ("🟢" if st == "approved" else "🔴")
                    label = f"{icon}  {name}  ({did[:8]})  {ts}"
                    if st == "pending":
                        label += "  ← 承認待ち"
                    lb.insert(tk.END, label)
                    dev_ids.append(did)
                    if st == "pending":
                        lb.itemconfigure(tk.END, fg="#f38ba8")
                if not dev_ids:
                    lb.insert(tk.END, "（登録済み端末はありません）")
                    dev_ids.append(None)
                # 選択を復元
                if sel and sel[0] < lb.size():
                    lb.selection_set(sel[0])
                    on_select()
            except tk.TclError:
                pass

        def on_select(event=None):
            sel = lb.curselection()
            if not sel or dev_ids[sel[0]] is None:
                approve_btn.configure(state=tk.DISABLED)
                revoke_btn.configure(state=tk.DISABLED)
                return
            did = dev_ids[sel[0]]
            d   = _config.get("devices", {}).get(did, {})
            st  = d.get("status", "pending")
            approve_btn.configure(state=tk.NORMAL if st == "pending"  else tk.DISABLED)
            revoke_btn.configure (state=tk.NORMAL if st != "revoked"  else tk.DISABLED)

        def approve():
            sel = lb.curselection()
            if not sel or dev_ids[sel[0]] is None:
                return
            did = dev_ids[sel[0]]
            d   = _config.get("devices", {}).get(did)
            if not d:
                return
            d["token"]       = _new_token()
            d["status"]      = "approved"
            d["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_config(_config)
            self._log(f"[認証] 端末を承認しました: {d.get('name', did[:8])}")
            refresh()

        def revoke():
            sel = lb.curselection()
            if not sel or dev_ids[sel[0]] is None:
                return
            did  = dev_ids[sel[0]]
            name = _config.get("devices", {}).get(did, {}).get("name", did[:8])
            if not messagebox.askyesno("端末を抹消",
                    f"「{name}」の接続を抹消しますか？\n"
                    "次回からこの端末は接続できなくなります。", parent=dlg):
                return
            _config.get("devices", {}).pop(did, None)
            save_config(_config)
            self._log(f"[認証] 端末を抹消しました: {name}")
            refresh()

        lb.bind("<<ListboxSelect>>", on_select)
        approve_btn.configure(command=approve)
        revoke_btn.configure(command=revoke)

        def auto_refresh():
            if dlg.winfo_exists():
                refresh()
                dlg.after(2000, auto_refresh)
        auto_refresh()

    # ── スキャン ───────────────────────────────────────────────────────────────
    def _do_scan(self):
        if not _config.get("scan_dirs"):
            messagebox.showwarning("警告", "スキャンするフォルダを追加してください")
            return
        threading.Thread(target=self._scan_bg, daemon=True).start()

    def _scan_bg(self):
        self._log("スキャン開始...")
        n = scan_books(_config.get("scan_dirs", []))
        self._log(f"スキャン完了: {n} 冊登録")
        self.after(0, lambda: self._books_lbl.configure(text=f"登録: {n} 冊"))

    # ── ポート確認 ─────────────────────────────────────────────────────────────
    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return True
            except OSError:
                return False

    # ── サーバー起動/停止 ──────────────────────────────────────────────────────
    def _start_server(self):
        if not _config.get("scan_dirs"):
            if not messagebox.askyesno(
                    "確認", "スキャンフォルダが未設定です。\nこのまま起動しますか？"):
                return

        port = _config.get("port", 8765)
        if not self._port_available(port):
            messagebox.showerror("エラー",
                f"ポート {port} は既に使用中です。\n"
                "前回のサーバーが完全に停止していないか、\n"
                "別のアプリが使用しています。\n\n"
                "しばらく待つか、ポート番号を変更してください。")
            return

        # UIを即更新してからバックグラウンドで起動処理（スキャンがあるのでメインスレッドをブロックしない）
        self._start_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._dot.configure(fg="#f9e2af")
        self._status_lbl.configure(text="準備中...", fg="#f9e2af")
        threading.Thread(target=self._start_server_bg, daemon=True).start()

    def _start_server_bg(self):
        import traceback
        global _server_thread
        try:
            if not _books:
                self._log("スキャン中（初回）...")
                n = scan_books(_config.get("scan_dirs", []))
                self._log(f"スキャン完了: {n} 冊")
                self.after(0, lambda: self._books_lbl.configure(text=f"登録: {n} 冊"))
            else:
                n = len(_books)
                self._log(f"スキャン済み: {n} 冊")

            self._log("[DEBUG] TLS証明書確認中...")
            fp = ensure_tls_cert()
            self._log(f"  cert fingerprint: {fp[:16]}…")

            self._log("[DEBUG] UvicornThread 作成中...")
            host = _config.get("host", "0.0.0.0")
            port = _config.get("port", 8765)
            cert = str(CERT_PATH)
            key  = str(KEY_PATH)
            _server_thread = _UvicornThread(host, port, cert, key)
            self._log("[DEBUG] start() 呼び出し...")
            _server_thread.start()
            self._log("[DEBUG] start() 完了")

            # IPv6 デュアルリッスン（失敗しても本体は動く）
            global _server_thread_v6
            try:
                _server_thread_v6 = _UvicornThread("::", port, cert, key)
                _server_thread_v6.start()
                self._log(f"IPv6 HTTPS listen 開始 (:::){port}")
            except Exception as _e6:
                self._log(f"IPv6 listen: 起動失敗（無視）: {_e6}")
                _server_thread_v6 = None

            start_discovery_responder()   # LAN自動発見の応答を開始
            start_webrtc_signaling()      # WebRTC P2Pシグナリングを開始（Firebase未設定なら即終了）

            self._log("[DEBUG] IPアドレス取得中...")
            ip  = get_local_ip()
            url = f"https://{ip}:{port}"
            self.after(0, lambda: self._status_lbl.configure(
                text=f"起動中...  {url}", fg="#f9e2af"))
            self._log(f"サーバー起動中: {url}（HTTPS）")
            self._log(f"  登録冊数: {n} 冊")
            self.after(5000, lambda: self._check_server_alive(url))

        except Exception as e:
            tb = traceback.format_exc()
            self._log(f"[ERROR] 起動処理でエラー: {e}")
            self._log(f"[ERROR] {tb}")
            self.after(0, lambda: self._dot.configure(fg=FG_RED))
            self.after(0, lambda: self._status_lbl.configure(text="起動失敗", fg=FG_RED))
            self.after(0, lambda: self._start_btn.configure(state=tk.NORMAL))
            self.after(0, lambda: self._stop_btn.configure(state=tk.DISABLED))
            self.after(0, lambda: messagebox.showerror(
                "起動エラー", f"サーバーの起動に失敗しました。\n\n{e}"))

    def _check_server_alive(self, url: str):
        """起動5秒後にスレッドが生きているか確認。成功後にキャッシュ生成を開始する。"""
        if _server_thread and _server_thread.is_alive():
            self._dot.configure(fg=FG_GREEN)
            self._status_lbl.configure(text=f"稼働中  {url}", fg=FG_GREEN)
            self._browser_btn.configure(state=tk.NORMAL)
            self._log("サーバー起動完了")
            # サーバー起動確認後にキャッシュ生成を開始
            if _books and not _preloading:
                self._log("バックグラウンドでサムネイルキャッシュを生成します...")
                start_preload()
        else:
            err = (_server_thread.error if _server_thread and _server_thread.error
                   else "不明なエラー（ログを確認してください）")
            self._dot.configure(fg=FG_RED)
            self._status_lbl.configure(text="起動失敗", fg=FG_RED)
            self._start_btn.configure(state=tk.NORMAL)
            self._stop_btn.configure(state=tk.DISABLED)
            self._log(f"[ERROR] サーバーが起動できませんでした: {err}")
            messagebox.showerror("サーバー起動失敗",
                f"サーバーを起動できませんでした。\n\n{err}")

    def _stop_server(self):
        global _server_thread, _server_thread_v6
        if _server_thread:
            _server_thread.stop()
            _server_thread = None
        if _server_thread_v6:
            _server_thread_v6.stop()
            _server_thread_v6 = None
        self._dot.configure(fg=FG_RED)
        self._status_lbl.configure(text="停止中", fg=FG_DIM)
        self._start_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
        self._browser_btn.configure(state=tk.DISABLED)
        self._log("サーバーを停止しました")

    def _open_browser(self):
        """既定ブラウザで内蔵ビューワーを開く（?token= でCookie認証を通す）。"""
        if not (_server_thread and _server_thread.is_alive()):
            messagebox.showinfo("ブラウザで開く", "先にサーバーを起動してください。")
            return
        port  = _config.get("port", 8765)
        token = _browser_token()
        webbrowser.open(f"https://127.0.0.1:{port}/?token={token}")
        self._log("ブラウザでビューワーを開きました")

    # ── 閉じる / 最小化 / システムトレイ ──────────────────────────────────────
    def _make_tray_image(self):
        """トレイ用アイコンを読み込む（無ければ本のシルエットを生成）。"""
        try:
            return Image.open(TRAY_ICON_PATH).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            img = Image.new("RGB", (64, 64), (24, 24, 37))
            d = ImageDraw.Draw(img)
            d.rectangle([16, 10, 50, 54], fill=(137, 180, 250))   # 表紙
            d.rectangle([16, 10, 26, 54], fill=(203, 166, 247))   # 背表紙
            d.rectangle([30, 20, 46, 23], fill=(24, 24, 37))      # 帯
            return img

    def _ask_action(self, title, prompt, o1_label, o1_val, o2_label, o2_val):
        """2択（+「次回も記憶」）ダイアログ。戻り値 (選択値 or None, 記憶するか)。"""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.configure(bg=BG)
        dlg.transient(self); dlg.grab_set(); dlg.resizable(False, False)
        res = {"val": None, "remember": False}
        tk.Label(dlg, text=prompt, bg=BG, fg=FG, font=("Yu Gothic UI", 10),
                 wraplength=340, justify="left").pack(padx=22, pady=(18, 10))
        remember = tk.BooleanVar(value=False)
        tk.Checkbutton(dlg, text="次回もこの動作にする（設定ファイルで変更可）",
                       variable=remember, bg=BG, fg=FG_DIM, selectcolor=PANEL,
                       activebackground=BG, activeforeground=FG,
                       font=("Yu Gothic UI", 9)).pack(pady=(0, 10))
        bf = tk.Frame(dlg, bg=BG); bf.pack(pady=(0, 16))
        def choose(v):
            res["val"] = v; res["remember"] = remember.get(); dlg.destroy()
        tk.Button(bf, text=o1_label, width=12, bg="#1a472a", fg=FG_GREEN,
                  relief="flat", font=("Yu Gothic UI", 9), pady=4,
                  command=lambda: choose(o1_val)).pack(side=tk.LEFT, padx=6)
        tk.Button(bf, text=o2_label, width=12, bg="#2a2a4a", fg=ACCENT,
                  relief="flat", font=("Yu Gothic UI", 9), pady=4,
                  command=lambda: choose(o2_val)).pack(side=tk.LEFT, padx=6)
        tk.Button(bf, text="キャンセル", width=8, bg="#3a0a0a", fg=FG_RED,
                  relief="flat", font=("Yu Gothic UI", 9), pady=4,
                  command=dlg.destroy).pack(side=tk.LEFT, padx=6)
        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.wait_window(dlg)
        return res["val"], res["remember"]

    def _on_close_window(self):
        """× ボタン: 設定に従い「終了」か「トレイ格納」。"ask" なら毎回確認。"""
        action = _config.get("on_close", "ask")
        if action == "ask":
            val, remember = self._ask_action(
                "ウィンドウを閉じる",
                "MangaServer を終了しますか？\n"
                "「トレイに格納」を選ぶと、サーバーを動かしたまま常駐します。",
                "終了", "exit", "トレイに格納", "tray")
            if val is None:
                return
            if remember:
                _config["on_close"] = val; save_config(_config)
            action = val
        if action == "tray":
            self._hide_to_tray()
        else:
            self._quit_app()

    def _minimize_action(self):
        """最小化ボタン: 設定に従い「最小化」か「トレイ格納」。"ask" なら毎回確認。"""
        action = _config.get("on_minimize", "ask")
        if action == "ask":
            val, remember = self._ask_action(
                "最小化",
                "ウィンドウを最小化しますか？\n"
                "「トレイに格納」を選ぶと、タスクバーから消えて常駐します。",
                "最小化", "minimize", "トレイに格納", "tray")
            if val is None:
                return
            if remember:
                _config["on_minimize"] = val; save_config(_config)
            action = val
        if action == "tray":
            self._hide_to_tray()
        else:
            self.iconify()

    def _hide_to_tray(self):
        """ウィンドウを隠してシステムトレイに常駐させる。"""
        if not _TRAY_AVAILABLE:
            messagebox.showinfo(
                "システムトレイ",
                "トレイ常駐に必要な pystray が見つからないため、最小化します。")
            self.iconify(); return
        self.withdraw()
        if self._tray is None:
            menu = pystray.Menu(
                pystray.MenuItem("表示", lambda *_: self.after(0, self._show_window),
                                 default=True),
                pystray.MenuItem("終了", lambda *_: self.after(0, self._quit_app)),
            )
            self._tray = pystray.Icon("MangaServer", self._make_tray_image(),
                                      "MangaServer", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        self._log("システムトレイに格納しました（トレイアイコンから復帰）")

    def _show_window(self):
        """トレイからウィンドウを復帰させる。"""
        if self._tray is not None:
            self._tray.stop(); self._tray = None
        self.deiconify(); self.lift(); self.focus_force()

    def _quit_app(self):
        """トレイとサーバーを片付けてアプリを終了する。"""
        if self._tray is not None:
            try: self._tray.stop()
            except Exception: pass
            self._tray = None
        global _server_thread
        if _server_thread:
            try: _server_thread.stop()
            except Exception: pass
            _server_thread = None
        self.destroy()

    # ── ログ ───────────────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        _log_queue.put(msg)

    def _poll_log(self) -> None:
        try:
            while True:
                msg = _log_queue.get_nowait()
                self._log_box.configure(state=tk.NORMAL)
                self._log_box.insert(tk.END, msg + "\n")
                self._log_box.see(tk.END)
                self._log_box.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


if __name__ == "__main__":
    App().mainloop()
